# Style production

For a production with `**Kind:** style`, these instructions take precedence over
`~/.claude/commands/showrunner/produce.md`. Rules live in `~/rust/nate_style` and
the repository's local rule directories. This document holds the procedure.

## Tiers

| Tier | Coverage |
| --- | --- |
| A | A full clippy, mend and rustfmt lint record per synced tip; a merge's quick gate is insufficient. |
| B | A worker judges every listed generator site or `pre_filter` hit; a partial generator also needs the uncovered read as C. |
| C | A worker reads each included Rust file against the rule. |
| D | One check per file in reach and listed site path; manifest rules check each crate. |
| E | Governs how fixes are made; never swept or counted as coverage. |

## Families

One unit director per family applies an exception decided in one crate the same
way in every crate. Use `rules.py list` for the current classified rules and
canonical ids. A repository-local rule of tier B, C or D declares the family
it joins; a tier A or E rule, local or global, has no family.

| Unit | B: listed rules | C: file reads | D: crate checks |
| --- | --- | --- | --- |
| `<style slug>-names-unit` | dont-repeat-type-name-in-fields, dont-repeat-enum-domain-in-variant-names, avoid-repeated-field-affixes, forbidden-words | spell-out-names, name-bindings-to-match-parameters, prefer-type-named-fields-and-bindings, comments-name-code-constructs | — |
| `<style slug>-values-unit` | never-prefix-unused-fields-or-variables-with, import-constants-at-the-top, derive-test-values-from-production-constants, test-module-allow-boilerplate, agent-must-review-allows, always-use-nextest | no-magic-values, constants-file-organization, prefer-functional-patterns, tests-live-with-the-type-under-test, prefer-observers-over-polling, system-params-carry-their-behavior | — |
| `<style slug>-types-unit` | enums-over-bool-for-owned-booleans, if-else-chains-signal-missing-types, dont-create-traits-for-single-implementations, prefer-from-impl-over-named-constructors, public-failures-return-named-errors, bevy-plugin-ownership, bevy-reflection-registration, extract-observer-run-conditions, prefer-events-over-messages, use-bevy_kana-in-all-bevy-crates | make-invalid-states-unrepresentable, uncovered components of its partial B rules | — |
| `<style slug>-modules-unit` | — | — | module-roots-as-table-of-contents, name-submodules-after-anchor-types, workspace-dependencies, module-structure, when-to-split-a-module, split-by-type-ownership, types-live-with-their-behavior |

A unit id is `<style slug>-<family>-unit`, where the style slug is `<repo>-style`.
Old `<family>-unit` ids remain valid. Every partial B rule also has a C read
in its own family.

## Family rank

`FAMILY_RANKS` in `scripts/style/rules.py` is the one rank table: `types`,
`values`, `names`, highest first. The planner and filing compare ranks only
through `rules.ranked_above`. A new family takes its rank in that table.
`modules` has no rank; structure blocking orders it, as below.

**Hold.** On each file, a family waits while a family ranked above it has open
work at that file. Only a family with a unit counts, and only in that unit's
pilot scope. Open work at a file is:

- a cell in state `open`, `stale` or `awaiting merge`;
- a filed finding, whose decision is unanswered, or approved and not built;
- an unfiled finding judged at the file that the synced tip has.

A waiting cell gets no ticket, and a waiting approved finding gets no proposal
ticket. Neither is dropped. A queued ticket that the rank holds is dropped
(`dropped <id>: <file> has open <higher family> work`), and its cells and
findings wait as above. Refill prints one line for each waiting file that
holds the unit's work: `waiting <family>: <file> has open <higher family> work`.
Filing keeps a waiting finding unfiled and prints the same line, so its decision
does not go to the user.

**Judge again.** When the higher work lands, the file's blob changes and every
cell at the file goes `stale`. The higher family judges the file again first,
then the lower cell gets a ticket. Where a family above has a unit, a
lower-rank finding judged at an older file is not filed or built: filing prints
`waiting <finding>: <file> changed since it was judged`, and refill prints
`waiting <decision>: <finding> <file> changed since it was judged`. A finding
counts as judged at the file that its holding ticket read, or that its last
declaration follow read. A finding whose own site is proved unchanged (the same line
and owner; for types and modules the whole unchanged item) follows to the new
file and goes on; a finding whose subject is proved gone is withdrawn. Every
other changed finding waits for judgment. A cell verdict never carries this way. A ticket that holds the same change again takes the
finding over, so the finding is judged at the new file and goes on. A judgment
of the changed file by another ticket that does not hold the finding (for a
followed finding, any later judgment of its new file) settles
it: filing withdraws an unfiled finding (`judged again in judgment <id>`), and
refill waives an approved one (`<file> judged again in judgment <id> without
it`). The withdrawal applies to every unfiled finding, of any family.

## Fix sequencing

Fixes run in the order that makes each fix simplest to write: a rule whose fix
changes code that another rule reads goes first. The planner enforces only the
family rank and structure blocking today. The order below is the target. Each
edge is a hypothesis until pilot data supports it.

**Order.** Tier E (`fix-root-causes-never-workarounds`,
`public-api-changes-require-explicit-approval`) has no position; it governs
every fix.

0. Tier A baseline: rustfmt, `clippy --fix`, `mend --fix`.
1. Dead code and Bevy flow: never-prefix-unused-fields-or-variables-with,
   prefer-events-over-messages, prefer-observers-over-polling,
   system-params-carry-their-behavior. These values rules change types and
   registration, so they go before types.
2. Types, in this order: no-duplicate-implementations,
   one-mechanism-per-behavior, crate-apis-expose-behavior,
   dont-create-traits-for-single-implementations,
   make-invalid-states-unrepresentable, public-failures-return-named-errors,
   enums-over-bool-for-owned-booleans, enums-over-option-when-none-has-meaning,
   if-else-chains-signal-missing-types, prefer-from-impl-over-named-constructors,
   use-bevy_kana-in-all-bevy-crates, bevy-reflection-registration,
   extract-observer-run-conditions, bevy-plugin-ownership. Then tier A cleanup.
3. Module placement: behavior-lives-in-its-most-cohesive-crate,
   types-live-with-their-behavior, when-to-split-a-module,
   split-by-type-ownership, module-roots-as-table-of-contents. Then `mend --fix`.
4. Values: tests-live-with-the-type-under-test, no-magic-values,
   derive-test-values-from-production-constants, import-constants-at-the-top,
   prefer-functional-patterns, test-module-allow-boilerplate,
   agent-must-review-allows (last). always-use-nextest takes any position.
   Then tier A cleanup.
5. Names: spell-out-names, dont-repeat-enum-domain-in-variant-names,
   name-submodules-after-anchor-types, prefer-type-named-fields-and-bindings,
   dont-repeat-type-name-in-fields, avoid-repeated-field-affixes,
   name-bindings-to-match-parameters, comments-name-code-constructs,
   forbidden-words.
6. Layout, sort only: constants-file-organization, module-structure.
   workspace-dependencies takes any position after use-bevy_kana-in-all-bevy-crates.
7. Tier A final: rustfmt, clippy, mend.

**Ownership first.** Duplication removal and crate placement go first in
their layers: delete the copies and move code to the crate that owns it before
any other rule touches that code, so no later fix is written twice or in the
wrong place. behavior-lives-in-its-most-cohesive-crate is a modules rule, and
structure blocking already runs it before types, values and names on a file.

**Module moves** go after types and before values (layer 3): they read the final
set of types, and tests and constants read the final file shape.
name-submodules-after-anchor-types runs with names, after the type renames.
module-structure runs last, on the final content. A module move changes paths in
every file that uses the moved items, so it holds its crate.

**Tier A tool fixes** run first, after each layer, and last. They are cheap and
give the same result when run again. mend runs after layer 3, because a module
move changes every path and visibility that mend reads. agent-must-review-allows
deletes an allow and runs clippy, so clippy follows it.

**Pilot data.** `order_waste.py` on the cargo-liner pilot (ledger copy of
2026-10-27, 1,182 merged tickets, 133 with code changes) counts two kinds of
waste for a rule pair (A, B). A ticket's change is its net change from its base
to its checkpoint. Work that its merges brought in is not part of it. A stale
finding is a finding of B that ended because its file changed: withdrawn as
judged again, withdrawn as declaration gone, or waived by the ledger, with a
merged fix of A to its file between its creation and its end. A reworked hunk
is a hunk of A that changed lines a merged fix of B wrote earlier. Person, unit
and unrecorded waives, exception withdrawals and bare rename duplicates are
decisions or bookkeeping, so they do not count.

| A first | then B | stale | reworked | analysis |
| --- | --- | ---: | ---: | --- |
| make-invalid-states-unrepresentable | if-else-chains-signal-missing-types | 0 | 67 | position only |
| make-invalid-states-unrepresentable | names family reads | 0 | 12 | family rank |
| if-else-chains-signal-missing-types | names family reads | 0 | 7 | family rank |
| enums-over-bool-for-owned-booleans | if-else-chains-signal-missing-types | 0 | 5 | position only |
| module-roots-as-table-of-contents | prefer-type-named-fields-and-bindings | 5 | 0 | layer 3 before 5 |
| make-invalid-states-unrepresentable | comments-name-code-constructs | 0 | 3 | edge |
| comments-name-code-constructs | forbidden-words | 0 | 1 | edge |
| dont-repeat-enum-domain-in-variant-names | forbidden-words | 1 | 0 | edge |
| split-by-type-ownership | comments-name-code-constructs | 1 | 0 | edge |

The data supports these orders. The strongest is new: in layer 2,
make-invalid-states-unrepresentable and enums-over-bool-for-owned-booleans go
before if-else-chains-signal-missing-types. The types unit ran if-else-chains
tickets on cargo-mend files first; make-invalid-states tickets then rewrote
the enums and matches those tickets wrote, 67 hunks in 12 files. The analysis
gives this only by list position, not by an edge.

Two pairs go against the order. derive-test-values-from-production-constants
changed 4 hunks in 2 files that if-else-chains-signal-missing-types wrote
(types go first): its fix put constants into new test lines, which running it
first would not have prevented. One module-roots-as-table-of-contents move made one
make-invalid-states-unrepresentable finding stale (types go first).

Every other edge is logical only. 8 edges have both sides in the pilot data and
no waste, among them make-invalid-states-unrepresentable before
enums-over-bool-for-owned-booleans. 53 edges have a side with no merged fix or
ended finding in the pilot, so the data cannot see them: both user pairs
(enums-over-option-when-none-has-meaning before prefer-functional-patterns;
tests-live-with-the-type-under-test before derive-test-values-from-production-constants
and the test reuse line of no-magic-values), every tier A edge (tier A has no
tickets), and every edge between two values rules. Names read tickets record
no rule for most changes, so their waste counts as `names read`.

**Conflicts.**

1. prefer-type-named-fields-and-bindings and name-bindings-to-match-parameters
   flip a binding passed straight to a parameter. Run type-named first, and
   skip such bindings in it; name-bindings wins there.
2. prefer-type-named-fields-and-bindings, dont-repeat-type-name-in-fields and
   avoid-repeated-field-affixes settle each case by an exception only in that
   order; another order can change a field twice.
3. derive-test-values-from-production-constants, or the test reuse line of
   no-magic-values, before tests-live-with-the-type-under-test loops: the
   writer widens a constant for a test (forbidden), mend narrows it, the test
   fails. Only tests-live-with first avoids it.
4. when-to-split-a-module before types-live-with-their-behavior: a split, then
   a relocation that empties a submodule, then a collapse. Each fix is undone once.
5. prefer-functional-patterns before the bool and Option type rules: a
   combinator hides the state the type rule names, and the type rule undoes it.
6. public-failures-return-named-errors changes a public signature each time, so
   each fix needs approval under public-api-changes-require-explicit-approval.

**Each roll-out checks the order.** After the pilot of every new repository,
run:

```bash
~/.claude/scripts/lib/py ~/.claude/scripts/style/order_waste.py --ledger <copy of the ledger> --repo <checkout> [--json]
```

It reads both and writes nothing. It prints one row per rule pair, sorted by
total waste, with where the analysis puts A, then the analysis edges with no
observed waste, split into seen and not seen. Add an order only when that
repository's data supports it, and record the counts here.

## Ticket queue

One ticket per phase:

- **Read:** one family, about 5,000 lines in one module subtree, 10–25 files.
  The Work Order lists each file's line windows (at most 300 lines, test module
  apart); judge and hold window by window, module docs first.
- **Listed:** one rule and crate; pack whole files in path order into tickets of
  at most 150 sites. A file with more than 150 sites gets one ticket by itself.
  Never split a file's sites between tickets.
- **Structure:** a small batch from one crate, at most 6 files and 3,000 lines,
  keeping directories together when they fit. Judge and record each file against
  every rule; the gate refuses the ticket while a file lacks a verdict.
  Structure tickets opened before this change keep their crate cells until
  they merge.

Run tier D first. Structure crates are taken in order of the other-family
pending cells they block inside a unit's pilot scope, most first, so a crate
blocking a pilot unit comes first. A repair phase always precedes a new ticket.
After an interruption, resume the assigned or checkpointed ticket from
`style_ledger.py ticket open --repo <checkout> --unit <unit>`; never take a second
one. A checkpointed ticket waits for its merge into the style branch before
refill. Clear the cargo-liner backlog before hana.

Standing plans carry `> **Standing:** style_plan.py` and live under
`docs/style-production/units/`. Git-exclude them: the ledger is their record,
and checkpoint and shrink never commit or shrink them. Keep production
instructions and plans out of `docs/style/` and `docs/<member>/style/`, which
`rules.load_rules` reads as local rules.
`add_unit.py` launches a standing plan from the showrunner checkout and copies
it into the unit worktree, where the unit's copy is its own. Refill, ticket
phases and pinned rule files live in that unit copy; the showrunner copy is
only the first-launch template. Edit a launched unit's copy for changes meant
for it. Checkpoint stages only code paths and never the plan or anything under
`docs/style-production/`; standing plans skip shrink and every closing gate.
Run refill at startup, after restart or compaction, and after each ticket.
Before selecting a new ticket, refill waits for sync if the branch point's
pushed tip is not an ancestor of the ledger's synced tip; an open ticket still
resumes or waits for merge. The pushed tip is `origin/<branch point>`, or the
local branch when the repository has no `origin`. The checkouts share one
repository, so the local branch can hold a parent merge whose gate is still
running. Reconcile and the hand-off merge check read the same pushed tip.
Report a waiting state once and end the turn blocked until sync, a board retry
message, or `ticket <id> merged; refill.` wakes the director. `ticket merge`
sends that wake to every unit waiting for sync. Refill records its wait before
checking once more for work; delivery clears only that wait, preserving a later one.

A ticket with no code changes makes no commit: record its cells at HEAD, verify
`git merge-base --is-ancestor HEAD refs/heads/<style merge branch>`, then write
the checkpoint procedure's atomic committed-awaiting-release record for that
ancestry-checked HEAD with `--ticket <ticket>`, run
`style_ledger.py ticket checkpoint --repo <unit-worktree> --id <ticket> --commit <HEAD> --session-dir <run folder> --phase <phase>`
and `style_ledger.py ticket merge --repo <unit-worktree> --id <ticket>`, which
releases the held reservation. Act on
its `not built` lines as after a showrunner's `ticket merge` (**Merging back**). Mark the unit's phase done,
report `From <unit>: ticket <id> closed with no code change; <n> held.` and
refill at once. If ancestry fails, leave the ticket assigned and report it.
The gate and `ticket checkpoint` refuse any ticket with an owned cell that is neither recorded nor removed, and name each one.
When the ticket's own commits delete a cell's file, record that cell with `style_ledger.py record --verdict removed`
(same flags as any record). The ledger finds the deleting commit with git and prints the cell's line,
`<rule> <path>: removed by ticket <id> at <commit7>`; the cell reads the same in `ticket open` and the style report.
Edits to the file that a sync merge brought in before the delete do not matter. The record refuses a file still in
the tree, a file deleted before the ticket's start, a delete that a merge brought in, a cell outside the
ticket, and a crate folder. A removed cell is closed, not clean; a later file at that path is a new cell. A delete
by any ticket counts: no verdict on a commit before it judges the new file, and the gate and `ticket checkpoint`
name the cell as unjudged until the ticket records it again.

Refill only the unit worktree's own plan copy:

```bash
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py next-phase --unit <unit> --plan <unit-worktree>/<Plan cell> --repo <checkout>
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py queue --repo <checkout> [--unit <unit>]
```

`next-phase` returns 0 after writing a phase, resuming an assigned ticket, or
reporting `repair first`; 3 means `waiting for sync`, or
`waiting for structure: <crates>` when every pending family cell in those
in-scope crates is blocked by a structure cell, until the
modules unit's structure tickets merge
(an unclassified catalog also says `pull nate_style`); 4 means
`board unavailable: <reason>`; 5 means
`waiting for merge: <ticket> checkpointed at <commit7>`; 6 means
`merge the style branch first: <n> cells judged at <unit tip7> wait for <style branch>`.
On 6, run the standing style unit merge in the unit worktree
(`git -C <unit-worktree> fetch origin <style merge branch> && git -C <unit-worktree> merge --ff-only origin/<style merge branch>`),
then refill. If the fast-forward is refused or leaves HEAD unchanged, do not
refill: report it to the showrunner.
Unknown reservation
availability stops selection without recording work. `queue` is a read-only
preview and also returns 4 when the board is unavailable.
A file cell waits only for open or stale structure cells at its own file; a crate cell waits for every one in its crate.

Refill leaves out two kinds of stale cell. The first is a held cell whose site
did not change since its held judgment, when at least one of its findings is
not waived, built or withdrawn, and each such finding is filed on an open
proposal. The second is a cell of the unit's family (or a structure cell, for
the modules unit) in its pilot scope whose newest judgment (current rule text,
newest declared scope) is at a commit in the unit tip and in the style branch,
and saw the inputs that the unit tip has: a ticket at that tip records the same
verdict again. Ranking and structure blocking still count such a cell. When no
work is left, refill returns 6 and records no sync wait only when a style
branch is stored, the unit tip is in the pushed style tip
(`origin/<style branch>` when origin exists, else the local branch), and that
tip changes the inputs of at least one such cell. Otherwise it returns 3,
`waiting for sync`: the next sync or style merge changes the cell.

`record --verdict held` requires an unfiled or filed finding on that rule's cell.
Without one, the ledger refuses the verdict and names the cell's waived, built
and withdrawn findings. A waived finding is the user's decision and does not
make the cell held. Structure tickets list waived findings separately: do not
hold them again. Record `waived` for a cell whose only findings are waived,
or hold a new finding with `held add` before recording `held`. A `waived` verdict
expires when the cell's rule text, file or context changes. Refill can then
offer the cell again; it adds no skip based on waived findings alone.

The ticket stores its rule text. Workers read the pinned files under the unit
worktree's `docs/style-production/rules/`, including after an interruption;
later rule edits cannot change an assigned ticket's Work Order. A queued ticket
is recovered under the same id if its files, and for a proposal ticket each file
its findings' changes edit, remain free; otherwise it is dropped
before selecting fresh work. Merged and dropped ticket phases are discarded on
refill; a checkpointed ticket waits for the style showrunner to merge it.
Refill writes plans and pinned texts atomically. Resume checks every pin's blob
and rebuilds a damaged ticket phase from its recorded instructions. Before
publishing a phase, it records the unit branch's starting commit once in the
ledger ticket's `base` field. The footprint gate uses `--worktree` and measures
from the later of that commit and the unit's merge-base with the style merge
branch, so style-branch work the unit took in is never judged as ticket work.
The gate judges the uncommitted tree the checkpoint will stage, using a temporary
index; it never changes the real index. No private Git ref
keeps a dropped ticket's abandoned commits reachable.

## Pilot scope

Create a restricted pilot with repeated `add_style.py --pilot <unit>[=<package>,...]`.
The selector accepts a family (`names`), its old id (`names-unit`) or its full
id (`cargo-liner-style-names-unit`). `--check` prints full unit ids.
Only named units get Units rows; all four standing plans are written. A unit
without a scope covers every crate. Package names map to workspace member
directories through `rules.workspace_members`.

The plan's Delegation Context carries `- **Pilot scope:** cargo-handler, cargo-port`
below its Family snapshot. Before first launch, edit the showrunner copy;
an interrupted bootstrap rewrites only unlaunched units' rows and scope lines.
After launch, only the unit's own copy counts:
`<main checkout parent>/<style slug>-<family>/docs/style-production/units/<style slug>-<family>-unit.md`.
Read the plan path from the Units row's Plan cell relative to that worktree;
a unit rename keeps its existing plan filename.
`next-phase` honors the `--plan` copy it receives for coverage and approved
proposals. An out-of-scope approved proposal stays approved and unassigned;
coverage selection proceeds.

## First files

Add `- **First files:** crates/cargo-port/src/one.rs, crates/cargo-port/src/two.rs`
to the Delegation Context in the unit's own plan copy, beside `**Pilot scope:**`.
Use repo-relative paths, separated by commas. These files get separate read
tickets before the other files in their group. Tickets that include these files
rank first. This line filters nothing. It has no effect once those files are
judged. Both `queue` and `next-phase` read the unit's own copy.

## Ledger

One style production and one synced branch point per repository. The ledger is
`~/.local/state/style/<repo>/ledger.sqlite`; only `style_ledger.py` writes it.
Startup runs `style_ledger.py reconcile --repo <checkout> --branch-point <branch>
--style-branch <style branch>` and logs its change lines. Act on its `not built`
lines as after `ticket merge` (**Merging back**). Reconcile recovers work
state from Git without advancing a tip already associated with that style branch.
When initializing or changing the style branch, reconcile syncs the ledger at
that branch's commit, falling back to the branch-point tip only if the style
branch does not exist. Creating a production likewise syncs at the style branch's
commit; a fresh branch takes that commit from the branch point. An interrupted
bootstrap already synced at that tip and branch leaves the row untouched.

Rule ids are `global:rust/<rule>.md` and
`local:docs/<member>/style/<rule>.md`. A member-local rule governs its member
crate; a root `docs/style` rule governs the repository. Unclassified rules have
no cells or tickets; show their count with the action **pull nate_style**.

The Rust cell universe contains tracked, included non-vendor `.rs` files.
Listed rules also gain cells for tracked non-Rust files holding their sites;
those cells use file fingerprints regardless of the rule's input scope.
Tier D has file cells; manifest-scope D rules keep crate cells. Sources outside
workspace members belong to crate `.`.

| Cell state | Meaning |
| --- | --- |
| `open` | No judgment exists for the cell. |
| `current` | The rule text and input fingerprint match, and the checked commit is an ancestor of the synced tip; A instead requires its matching successful lint record with no diagnostic for the file. |
| `stale` | Previous coverage no longer qualifies: rule text, inputs or lint configuration changed, or the checked commit has no qualifying merge evidence. |
| `awaiting merge` | The checked commit is outside the synced tip but is an ancestor of the stored style branch's current tip, with matching rule text and inputs there. |

Bootstrap and sync store the style branch with
`style_ledger.py set-tip --repo <checkout> --tip <branch-point tip>
--style-branch <style branch>`. Omitting `--style-branch` preserves a stored one;
without one, unmerged judgments read `stale`.

Fingerprints use committed Git objects at the rule's `file`, `crate` or
`manifest` scope. A `clean`, `exception` or `held` verdict with `--input-scope file`
uses the file's own fingerprint. It stays current when other files in its crate
or module change. On a crate or module cell of a Rust file, these three verdicts
must give `--input-scope file` or `--input-scope rule`. With `--input-scope rule`,
use `--reads <files>` to name each other file the verdict rests on. The fingerprint
uses the cell's own file plus those named files, so only a change to one of them
brings the cell back. Without `--reads`, the fingerprint uses the whole crate or
module. Each named file must exist at the checked commit. The manifest scope covers the workspace root and member
manifests; crate `.` covers only its own files. A `modules` family judgment
also stays current when only comments changed: the same input files at the checked
commit and at the tip, and each changed `.rs` file has the same mode, the same code
tokens and the same inner docs (`//!` and `/*! */`, each at the same place in the code). A change to an ordinary comment or
to an outer `///` or `/** */` doc does not bring it back. A changed non-`.rs` input
(a `Cargo.toml` in crate scope), a changed file mode or submodule, an added or
removed file, or a file that does not tokenize brings it back. Every other family compares bytes, because names rules
judge comment text. Exceptions expire with their
inputs. Verdicts are `clean` (no change needed), `fixed` (fix applied),
`exception` (reviewed allowed case), `held` (proposal deferred) and `waived`
(change declined). Held and waived judgments count as current while their
text and inputs still match.
Tickets pin their rule text: `record --ticket <id>` uses that pin, and a supplied
`--rule-blob` must match it. A changed rule leaves an old ticket's judgment stale.
The phase of a proposal ticket lists the exact cells to record, one
``- `<rule>` · `<file>` `` line each. A tier B rule has a cell only at a ticket file
with one of its listed sites at the synced tip, because `record` refuses it at
other files. A `#uncovered` component has a cell at every ticket file.

A partial B rule has listed file cells and `<rule id>#uncovered` C cells for
every included Rust file in reach. Both components must be current to finish
the parent rule; pseudo-rules never increase the rule count. Zero listed sites
complete only the listed component. A whole B rule or a `pre_filter`-only rule
has no uncovered component. A partial D rule's file cell covers both reads.

`set-tip` fills listed sites from committed files. Coverage and site reads
refresh sites when the rule blob changes, even at the same tip; zero-site
markers distinguish an empty result from a rule never enumerated.

## Sync

Every successful parent merge runs the best-effort notification:

```bash
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_sync.py notify --repo-dir <checkout> --branch <branch point> --tip <new tip>
```

A missing production or another branch point sends nothing. Notification
failure reports one line and never fails the parent's merge.

On a sync message, run `style_ledger.py tip seen --repo <checkout> --tip <tip>`;
`seen` (including a reason) skips the following five steps. For a new tip, run `~/.claude/scripts/lib/py ~/.claude/scripts/style/style_sync.py own-handoff --repo <checkout> --tip <tip> --style-branch <style merge branch>`.
Exit 0 means the tip has the style branch's own tree (our hand-off merged back): it is now the synced tip and seen,
recorded with reason `own hand-off` (`tip seen` prints `seen (own hand-off: synced with no lint run)`). Merge the tip
into the style merge branch with no Merge tests (the tree does not change), wake sync waiters, and skip the five steps.
Exit 1 means the trees differ; run the five steps:

1. Merge the new tip into the style merge branch and run its Merge tests.
2. Run `~/.claude/scripts/lib/py ~/.claude/scripts/style/style_sync.py lint --repo <checkout> --checkout <style checkout> --tip <branch-point tip>`.
   This uses `bash ~/.claude/scripts/direct/verify.sh lint --workspace --no-cache --check`
   at the exact requested commit in a disposable checkout, with workspace mend,
   rustfmt and clippy (`--all-targets --all-features`, workspace lint config).
   Nothing rewrites the tree: mend runs in report mode and rustfmt with `--check`.
   Before verify starts, `checkout/target` links to `<main git dir>/style-sync-target`
   (about 2-3 GiB), shared by all syncs of that repo. `scratch.py hold` owns it under
   `run:<repo>-style` until the style production itself wraps (see Lifetime).
   Checkout cleanup unlinks the symlink and keeps the target. A missing or swept
   target is recreated for a cold run. Each sync takes a blocking exclusive flock
   on `style-sync-target/sync.lock` before creating its checkout and holds it
   through lint and cleanup. This keeps source timestamps newer than the previous
   sync's Cargo fingerprints. Cargo's build lock also serializes builds on the
   target; this starts no extra parallel build. The link avoids setting `CARGO_TARGET_DIR`,
   which would change sccache's key.
   It records per-file diagnostics once per tool; disabled or incomplete checks
   grant no successful coverage. Exit 0 means clean, every tool recorded passing;
   continue the sync. Exit 1 means diagnostics recorded or a tool did not pass;
   continue to `set-tip` and `tip done`. Exit 2 means an error (refusal, failed
   `lint-record` or unreadable checkout); stop the sync. Exit 143 or 129 means
   SIGTERM or SIGHUP stopped the run; stop the sync.
   Every exit the run can catch stops verify.sh and its processes, then deletes the
   checkout and removes its worktree registration, also a locked one. Each
   `style-sync-lint-*` dir holds `owner.json` (the run's pid and repo) and
   `verifier.pgid` (written by verify.sh's own process, which then starts verify.sh
   only while the owner runs). The owner
   record goes last; if the registration step fails, the dir keeps it and the next
   run retries. A run first removes the dir and registration of a run whose owner and
   verify.sh group are both gone; the dir stays while a killed run's verify.sh runs.
3. Run `style_ledger.py set-tip --repo <checkout> --tip <branch-point tip> --style-branch <style merge branch>`,
   then `style_ledger.py status --repo <checkout>`: changed inputs become stale,
   new files open. Report a nonzero unclassified count to the parent with
   **pull nate_style**.
4. Tell every unit director to merge the style branch at its next pause between
   tickets, and wake those waiting for sync to run `style_plan.py next-phase`
   against their own plan copies.
5. Run `style_ledger.py tip done --repo <checkout> --tip <branch-point tip>`.

The processed-tip shortcut skips only these five steps, never the ledger's
rule-blob site refresh before `status`, `cells` or `listed_sites`, including
reads from `next-phase`. A rule edited in `~/rust/nate_style` gets fresh sites
on the next read without a branch-point move or sync message.

When merging the synced style branch mid-ticket, recheck each cleanly merged
file's incoming diff before checkpointing and record its new fingerprint.
On conflict, drop every finding touching that file with every dependent edit
in other files; a multi-file rename goes as a whole. Release reservations no
remaining finding touches, rerun the gate and leave dropped cells stale.

## Merging back

Merge each unit checkpoint into the style merge branch as soon as it arrives;
the `ticket merge` after it releases its berth reservation. Use the ordinary
`merge_checkpoint.py --production <style doc> <unit> <phase> <hash> --started …`
with one `--also <path>` for each entry of the unit's open ticket's `files`
(`style_ledger.py ticket open --repo <checkout> --unit <unit>`). Its Owns is
`none — takes what the board leaves free`; paths outside that ticket stay refused.

A style ticket's checkpoint does not release its berth reservation: the unit's
reservation record holds it for the ticket, and `ticket checkpoint` keeps the
record's run folder and phase on the ticket.
After the merge, run `style_ledger.py ticket merge --repo <checkout> --id <ticket>`
to release that reservation and wake every unit waiting for sync, and send the owning unit director
`From the showrunner of <style slug>: ticket <id> merged; refill.`
For each `has no applied check` line that merge prints, check the change in the
code, and run the printed `held built` command when the change is there. A rerun
of `ticket merge` on a merged ticket prints these lines again. A finding whose
change is not there waits for a person, who makes the change and
runs that command, or waives the finding with
`style_ledger.py held waive --repo <checkout> --id <finding id> --person <name>`.
A merge that cannot release the reservation (the unit worktree moved off the
checkpoint, or berth did not confirm the release) exits 2 with
`ticket <id>: ... stays held; rerun ticket merge` and keeps it held; fix the
cause and run `ticket merge` again, or let the next `reconcile` retry it. A
merge after the release is confirmed releases nothing more.
The ledger accepts only a checkpoint contained by its stored style merge branch.
Then run
`style_sync.py send-handoff --repo <checkout> --slug <style slug> --checkpoint <style branch tip>`.
It adds the hand-off, sends
`From the showrunner of <style slug>: hand-off <hash>` to the current parent's
`showrunners.py address <parent>`, then records `handoff send`.
A send that send.py queued (`QUEUED:`, exit 1) is sent too: send.py delivers
it later. The command prints `hand-off <hash7>: queued for <parent>`.
Style work never waits for a sync to go home.

The parent correctness-reviews the hand-off's diff against the branch point
as for a unit checkpoint, then runs
`merge_checkpoint.py --from-child <style slug> --checkpoint <hash> --production <parent doc> --started <time>`.
The child must be attached, the checkpoint on its merge branch and descended
from its last merged hand-off. The subject is
`Merge <style slug> hand-off (<hash7>) into <branch point>`.
After pushing, notifying sync and promoting, the attached receiver records the
hand-off as merged in the style ledger with its merge commit, then sends
`From the showrunner of <parent slug>: hand-off <hash7> merged as <merge7>.`
The acknowledgment is a notice; only after a `hand-off ledger:` line run
`style_ledger.py handoff merged --repo <checkout> --checkpoint <hash> --merge-commit <merge>`;
ancestry on the branch point is the evidence, so it also takes a pending row.
The receiver decides delivery state once. A local merge missing from origin is
pushed, notified and promoted before acknowledgment, without merging again. A
checkpoint already on origin merges, pushes, notifies and promotes nothing: it
prints
`hand-off <hash7> already merged as <merge7>` and acknowledges again.
A failed first push undoes the unpushed merge; no acknowledgment precedes a
successful push. Hand-offs run the same package and Merge tests and test log
as unit checkpoints.

At startup, first run
`style_sync.py resume --repo <checkout> --slug <style slug> --branch-point <branch point> --style-branch <style merge branch>`
and append its reconcile change lines to **Log**. Act on its `not built` lines as
after `ticket merge`. Reconcile stores the branch
point before any acknowledgment and marks merged hand-offs by ancestry.
Replay proceeds oldest first: pending notices are sent then recorded; sent
notices are sent again; a notice sent to another parent is sent to the current
parent then rerouted. A queued send counts as sent; failed sends leave the row
unchanged.
`style_ledger.py handoff reroute --repo <checkout> --checkpoint <hash> --to <parent>`
accepts only sent rows, preserving their checkpoint and creation time; the
same parent is a no-op. `handoff send` still refuses a second parent.
Reattaching runs this resume after updating Parent production and Proposals.

While root, resume prints `root: merge <hash7> with --root-self` and sends nothing.
Correctness-review and merge each own hand-off with
`merge_checkpoint.py --from-child <own slug> --checkpoint <hash> --root-self --production <doc in Parent production> --started <time>`.
No other production writes that branch then. The root receives no
acknowledgment; the receiver prints
`root: run style_ledger.py reconcile to mark <hash7> merged`.
Run `reconcile` afterward: a root's unsent row stays pending until ancestry
marks it merged.
This also clears hand-offs a departing parent had not merged.

On `From <unit>: ticket <id> closed with no code change; <n> held.`, merge
nothing and send neither a hand-off nor a refill message: the unit refills
itself; its `ticket merge` already woke units waiting for sync.
When `<n>` is positive, run `style_proposals.py file`.

## Yielding

Selection skips files reserved on the berth board, changed on live units'
unmerged branches, or dirty in their worktrees. Acquire reservations when
editing begins. Product work always wins a conflict.

After a clean incoming merge mid-ticket, recheck the file's incoming diff before
checkpointing and record the new fingerprint. On a conflict or berth overlap,
drop every finding touching that file, including all its edits in other files;
a multi-file rename is dropped as a whole. Release reservations no remaining
finding touches, rerun the gate, and leave dropped cells stale for later work.
Record each dropped file with `style_ledger.py ticket yield --repo <unit-worktree>
--id <ticket> --path <file>`; this clears the ticket's verdicts and unfiled held
findings on it, leaving its cells for later work.
A proposal ticket yields the file of a finding whose file, or a file its change
edits, is blocked. Its findings at that file stay filed under their decision with
no build ticket, and the ticket no longer lists them. The gate and `ticket merge`
skip them, and a later proposal ticket takes them. The command prints
`yielded <finding id>: it stays filed for a later ticket` for each. It refuses a
file with no finding of the ticket, a file that holds every finding the ticket has
left (drop the ticket), and a kept finding that can need a yielded one. A `path`
attribute that mounts a file across crates refuses when either crate holds a
finding of the ticket, and a `path` attribute that mounts a file not named
`.rs` refuses anywhere. A `path` attribute on an inline module names the folder
its `mod` items read from; it refuses only when that folder is in another crate.
The module's own name stays a reading too, because a `cfg_attr` attribute can be
inactive.
Otherwise a kept finding in another crate passes. In a shared crate, each finding of both sides
must be a plain file rename; the yielded file must not name a kept module; the
new names must differ, and neither can be the other's old name; and no file of
the crate can re-export a renamed module, glob-import its parent, or hold an
`include`, `macro_rules` or `macro` word. The acceptance gate's `verify.sh` run
on the unit branch proves that the kept work builds without the yielded change.
A structure ticket cannot yield.
Style units obey build holds and memory admission like any unit.

## Big changes

Apply a fix without asking only when all mechanical criteria hold:

1. No file is added, deleted, renamed or moved, and no `mod` line changes.
2. No item reachable from another crate changes name, signature, fields or variants.
3. No type gains or loses a field, variant, generic parameter or trait impl,
   and no new type, trait or error type appears.
4. A rename's sites are in one crate and at most 10 files.
5. Each file in the finding has under 200 changed lines.
6. Behavior is preserved: no condition, literal value or control flow changes
   meaning, and no serialized, reflected or configured name changes (serde and
   reflect names, config keys, documented usage).

A finding of a class in `scripts/style/mechanical_classes.toml` is applied when its proof passes.
The proof must pass on the whole change: a change that adds an allow, or that
changes code beyond what the proof explains, stays held for the user. The user
never approves a finding of a listed class. The list names each class, its
proof and its rules, and the unit text and the ticket gate read it. The classes:

- `pure-move` (proof `item_move`; rules `module-structure`,
  `types-live-with-their-behavior`, `split-by-type-ownership`): one item (a
  const, static, fn, struct, enum, type alias or impl) goes to its owning module
  or to a named shared owner (`constants.rs`, the type's module) in the same
  crate. Its uses are re-pointed, and a re-export keeps every old path. It is
  mechanical also where criteria 1 and 3 bar it. `prove_item_moves` finds that
  only paths and `use` lines changed. The move is still held when the finding
  names no destination, or when the move makes an import cycle or crosses crates.
- `stale-allow-lint` (proof `lint_removal`; rule `test-module-allow-boilerplate`):
  a lint name goes out of an `allow` attribute in test code (a test file or a
  `#[cfg(test)]` item) when its pattern has no match in the attribute's scope,
  the same check as the `test_allow_boilerplate` generator. The whole attribute
  may go when it keeps no lint name. The reason and the other lint names stay.
- `binding-rename` (proof `binding_rename`; rule `prefer-type-named-fields-and-bindings`):
  a local binding, parameter or pattern name changes to its approved name at its
  declaration and each use in its scope. The proof accounts for struct shorthand
  and format captures, excludes shadowed bindings, and refuses collisions and
  macros whose names it cannot check.
  `prove_lint_removals` finds that only lint names were removed. An `expect`
  attribute is not in this class.

An uncertain result goes to review. Every other tier D finding and every
`mode: propose` rule is held. Remove non-mechanical findings and all dependent
edits from the applied diff; merge the remainder after its gate passes.
Record each held finding with `style_ledger.py held add` and its cell with
`record --verdict held`.

Each `style_ledger.py held waive --id <finding id>` requires exactly one actor:
`--ticket <judging ticket>` for the unit that found it, or `--person <name>` for
a person. The ledger records the actor supplied by the caller.

To correct the after text of a waived finding, use `style_ledger.py held replace
--id <waived id> --after <text>`. It accepts a unit's waive, keeps the id and
judging ticket, returns the finding to unfiled, and restores a newest waived
cell judgment to held. For an old waive with no recorded source, add
`--person <name>`; the ledger records who undid it, as `unwaive` does. A person's
waive and a ledger waive are always refused by `replace`. When there is no
waived cell judgment, record `held` for the cell after replacement. Plain `held add` keeps an existing
waive.

Run from the unit worktree, using the ticket's recorded start commit:

```bash
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_footprint.py check --repo . --base <commit id> --worktree
```

Exit 0 means every finding is `mechanical`; exit 1 prints `review: <reasons>`
for anything the filter cannot clear. Exit 2 prints `error: <reason>` when the
check could not run: a git failure, such as a missing revision, or a file
error. Without `--finding-map`, the whole diff is one finding. An optional JSON map assigns finding ids to file lists, or to
objects with `files` and `hunks` lists. A hunk is identified by its `path`,
`old_start` and `new_start` from a zero-context diff; every changed hunk must
have one owner. The gate folds merged-in parents into the comparison base so
their changes, like work the unit took in from the style merge branch, are not
judged as ticket work. A conflicting fold requires review.
Each finding must preserve behavior using only its own edits; another finding's
call-site changes cannot complete its rename. Changed documented usage, including
block documentation, requires review. Glob imports prevent proving a rename's
identity, and public glob re-exports prevent establishing module privacy.
The gate shortens constant paths only when the added import and each site share one module scope and both paths match.
A glob is allowed in a module, but not in a block around a changed site.
An added import may carry one `#[cfg]` if each site's item carries the same condition.
An external crate name counts as a root only when the file uses it only as a path root.
An edit may remove a module-level import whose name stays in the file as a plain word (for example a closure `|line|` after `use ratatui::symbols::line;` goes), only when the removed path is the parent of an import added in the same module, the name is snake case and not `drop`, a primitive type or a path keyword, no `use` is a glob, and each remaining word is outside attributes and macro token trees, with no `::` beside it and no `!` after it. The parent rule shows a module, which is never a pattern or a value, so the word names the same thing after the removal or the build fails. Residual: one `use` brings in every item with that name, so a module and a constant of the same name in one module make a pattern word a binding without a build error.
A trailing comma that rustfmt adds or drops at the end of a call's arguments, a parameter list, an array or a brace list does not count as a change. A comma inside a macro or an attribute, or at the end of a tuple, still counts: `(x,)` is a tuple and `(x)` is not, and a macro can tell `[1,]` from `[1]`.
It proves `Mode::Old` and `Self::Old` variant renames only when no other item has the enum's name.
Attributes or unsupported derives that observe variant names prevent proof.
It proves a local `let` rename only when its name is used after the statement, in its own block.
It clears a changed condition line only when its tokens match after proven rewrites.
It permits lint names to be removed from `allow` and `expect` attributes, including whole attributes.
Generic signature brackets and attributes on unrelated items alone do not
require review. Derive changes count as trait implementation changes.
Changes inside macro invocations require review even when only spacing changes;
type-shape checks include unions and their derives.

File held changes with `style_proposals.py file --repo <style checkout>
--proposals <folder> --parent <slug>`. Read the folder from the production's
`**Proposals:**` line and the parent from `showrunners.py parent <style slug>`.
Run filing after each checkpoint with held cells and each no-change notice
with held findings. Findings held by assigned or checkpointed tickets wait.
Findings judged under an older rule text stay unfiled until a judgment under
the current text judges their cell again; then filing withdraws them.
A `skipped <issue>: <reason>` line means that filing cannot write that
decision. When filing new findings, those findings stay unfiled. When
refreshing an open decision, its findings stay filed and the file stays as it
was. The other decisions still file. When it skips one or more decisions,
`file` exits 1. Fix the named decision file as the reason tells, then run
`file` again. A `skipped <finding>: rule changed since judged` line names one
finding, not a decision, and does not change the exit status.
Its cell has no judgment under the current text yet: a refill judges the stale cell,
and the next `file` withdraws the finding.

Filing first withdraws each filed finding whose site has an `exception` as the
newest judgment under its rule's current text, when the finding was judged
under an older text. `withdrawn` is its own state, not `waived`, and the
finding records that judgment's row. A finding that an open ticket builds is
kept. The judgment must also be newer than the finding's own judgment.
An unfiled finding judged under an older text is withdrawn when the newest
judgment at its site uses the current text. Any verdict withdraws it, and the
finding records that row. It has no decision, so no decision changes.
An unfiled finding of an open ticket waits for that ticket. A decision whose
findings are all withdrawn closes with a reason that names them
and the judgment rows. A decision that keeps other findings shows only those,
and `complete` closes it when they are built or waived.
`style_proposals.py withdraw --repo <style checkout> --proposals <folder>
[--dry-run]` runs this step alone. It prints
`withdraw <finding> <rule> <path>:<line>: judgment <row> <verdict> under <blob>`,
a `kept` line with the ticket, a `close` line per decision and a `totals` line.
The dry run prints the same lines and writes nothing. A decision that it cannot
close or refresh gets a `skipped <issue>: <reason>` line, the other decisions
go on, and `withdraw` exits 1 as `file` does.

Before that, filing reads the declaration of each unfiled and filed finding of
a `.rs` file. The declaration is the item that holds the finding's line at the
commit it was judged at, or at its last follow: its keyword, its name and the
impl or trait it is in. The step reads only a finding whose commit is in the
synced commit. When that file at the synced commit no longer declares the
item, the step searches every Rust file. One other file: the finding follows it
to the line with the same text in the item, else the item's first line, and
keeps its id. Two or more files, or a filed finding's file in another crate:
the finding stays as it is for a person to waive or fix in the code. No file:

- A filed rename is built when its own file, or the one file that gained it,
  now declares its after name with the same keyword and owner, and its decision
  is approved. A built rename follows to its new declaration, also in its own
  file. Two or more such files, or one in another crate, leave the finding as a
  move does. A rename in a decision that is not approved waits for the answer,
  with no line. The step reads the answer again when it writes.
- A finding whose item, or the impl or trait that holds it, has a new name from
  built rename or type findings of approved decisions in the same crate follows
  the item to its newest name, in its own file or in the one file that gained
  it, and keeps its id. The renames apply one after another; a name with two
  new names, or a rename back to an earlier name, stops the chain. A rename
  qualified by a module names only the items of that module's files and the
  impl items of a type declared there; `crate::` names the crate root. A rename
  qualified by a type names only the impl or trait items of that type under any
  of its names, and a `Self` path names the type of its own impl or trait. A
  trait path also names direct members of its readable trait impls. The member
  keeps its implementing type, including that type's approved new name. The
  destination must implement the same trait with the same generic arguments.
  Equal paths with equal arguments name the same trait. A bare name can match
  a qualified path through one declared trait or a plain file import, including
  an `as` alias. A block import or glob affects only impls inside that block.
  Conditional file imports, file globs, and re-exports block proofs for names they can bind.
  File-level `self::` and `super::` paths resolve through plain file-level `mod` declarations.
  Only `src/lib.rs` and `src/main.rs` name the crate root.
  Files under `src/bin`, custom target paths, conditional links, and `#[path]` modules grant no layout proof.
  Relative paths inside blocks or above the crate root grant no proof.
  The module path must match the trait's declared module. Built renames must name that
  trait at their own sites. A qualified path on the cited line uses that path's scope.
  A member declaration without that path uses its owning trait or impl header's scope.
  An uncertain trait identity keeps the finding
  unchanged. Ambiguous members grant no follow. Two or more gaining files, or a filed finding's
  file in another crate, leave the finding as a move does. A finding whose own
  rename names its item keeps the rule above; a filed rename whose impl type a
  built rename renamed is built at the renamed type.
- An item renamed outside the ledger can follow without a built decision.
  Its old name must be absent from every declaration in its crate, including test items.
  An independent source diff must map the site to its renamed item in the same file.
  Alternatively, exactly one changed file in that crate must gain one matching destination.
  The original file counts when checking for competing destinations. An existing twin grants no proof.
  The keyword, owner and proved trait identity must agree.
  At least half the old body's non-blank lines must remain, in order, after replacing only the renamed identifier.
  Body source includes internal comments. It excludes signatures, attributes and outer braces.
  Empty or unreadable bodies grant no proof. Changed literals, calls and indentation do not count as equal lines.
  A child follows only through its proved renamed owner and its own retained name and lexical scope.
  A use of a local name does not prove a binding. Duplicate or unsupported bindings grant no proof.
  An outside follow keeps the finding's id, request, state and decision. It never marks it built.
  A refused outside search keeps the row, even when its source file is gone.
  Its `left` line names the mapped declaration, sorted candidate sites, body counts and failed checks.
  Earlier ambiguous-move, cross-crate, open-ticket and uncertain-trait refusals still apply.
- Another filed finding whose file is still there stays filed for a person. A
  person runs `style_ledger.py held waive --repo <checkout> --id <finding> --person <name>`, or
  puts the item back in the code.
- An unfiled finding, or a filed finding whose file is gone, is withdrawn, with
  no decision when it was unfiled.

A finding that an open ticket builds is kept. When the synced commit moves
after the step reads the declarations, it writes nothing and exits 1; run it
again. The step prints
`follow <finding> <rule> <path>:<line> -> <path>:<line>: <keyword> <name>`,
`built <finding> <rule> <path>:<line>: <keyword> <name> renamed to <new name> in <path>:<line>`,
`withdraw … declaration gone at <commit>`, `left … declared in <files>`,
`left … declared in another crate: <file>`, `left … declaration gone; waive it or
declare it again`, `kept …: open ticket <ticket> builds it`, a `close` line per
decision and
`totals: follow <n>, built <n>, withdraw <n>, left <n>, kept <n>, close <n>`. A
decision that it changes refreshes its sites, and one with no finding left
closes. A decision whose last finding the step built closes at the next refill
or `complete`, with `built in synced commit <commit>`. Each run refreshes
every open decision that holds a followed finding, so a rerun completes a run
that stopped before its refresh.
`style_proposals.py follow --repo <style checkout> --proposals <folder>
[--dry-run]` runs this step alone; it exits 1 when it leaves a finding or skips a
decision. `held list` shows `followed from <path>:<line> at <commit>` for a
followed finding, and `--json` adds `followed_from`, `followed_at` and
`declaration_gone_at`.

One style decision covers one rule and crate under one rule blob. An answer
also covers later findings under that rule text: a declined decision waives
them with its stored words and records the person actor `user`; `file` prints
`waived <n> findings in <decision>: declined: <words>`. An approved decision
that is closed opens again with its approval for a later finding when each of
its findings is built, waived or withdrawn. The ledger decides this, not the
close reason, so a decision closed by hand opens again too. A closed decision
with a filed finding refuses it: `file` prints
`skipped <decision>: closed decision has unbuilt findings` and exits 1. Open
that decision with `proposal_issue.py reopen <decision>` and file again. A
closed decision with no answer takes a later finding only when its close
reason starts with `built in ` or `nothing left to build: `; else `file` prints
`skipped <decision>: closed decision has no approval`. A new rule blob needs a
new answer. Each decision shows counts and up to three examples from different
files. The ledger holds its full site list. Show decisions only when the user
asks through `/showrunner:proposals` or `/adhoc_review`.

Approve with `proposal_issue.py approve <issue> --by user --showrunner <parent>
--unit <unit> --plan <unit's own plan copy>`. Do not add phase numbers.
Approval sets the decision's stage active (in progress), so it leaves the
footer's proposal count.
A rule whose frontmatter says `approval: always` never asks the user: `file`
creates each new decision under it approved, with the fields a user approval
writes. The unit and plan are the live family unit's Units row of the standing
production, `showrunner` and `approved_in` are the style slug, and `approved_by`
is `rule <rule id>`. It prints `approved <decision>: rule <rule id> is always
approved`. With no live unit of the rule's family, `file` prints `skipped
<decision>: rule <rule id> is always approved, but no <family> unit builds it`.
Each `file` also approves, the same way, every open decision under such a rule
that still waits for the user, whatever rule text it was filed under.
A decision that inherits an approval keeps it, a declined decision stays
declined, and reopen keeps the rule's approval. `unapprove` reads a rule
approval as the user's. Any other `approval` value stops the catalog loading.
Change an answered decision only with `proposal_issue.py unapprove`, `proposal_issue.py decline-answer` or
`proposal_issue.py reopen`; never edit its stage, unit or answer by hand.
Refill builds approved findings in small tickets before coverage work.
Each ticket permits its own approved findings and proven rename references.
Only a rename whose `before` and `after` are one `<parent>::<name>` pair proves
reference edits. A rename with no such pair (a bare name, a field or prose)
approves no reference edit, and the ticket's other pairs stay approved. A
ticket with a type or restructure finding approves no reference edit.
The ticket's phase names each approved pair. It tells the worker to apply any
other rename only in its own file, and to hold it when other files use its old
name.
Other findings stay held. Package checks still run. The parent schedules
changes to a live unit's files through `produce.md`'s `<CrossUnitChange>`.
Refill first runs the declaration step once, on the findings of all approved
decisions, so a finding tickets at the file its declaration moved to. A finding
that the step leaves gets `waiting <decision>: <finding> declared in <n> files`,
`waiting <decision>: <finding> declared in another crate: <file>`,
`waiting <decision>: <finding> declaration gone` or
`waiting <decision>: <finding> its function moved to <file>; <reason>`, and stays out of the ticket.
Each busy file of a decision gets `waiting <decision>: <path> busy`, and its free
files are still planned in the same run.
A finding whose change edits a busy file other than its own (for a file rename,
a file that names the renamed module; for a qualified rename, a `.rs` or `.md`
file that holds the old name; for a restructure of a `.rs` file, a file whose
`mod` line or `#[path]` string declares it, such as the owner of a test file, or
whose `path` attribute the planner cannot read with certainty)
gets `waiting <decision>: <finding> edits <file>, busy`
and stays out of the ticket; the other findings of its file stay available.
A file rename whose old module a file, its own file included, may declare by a
`path` attribute the planner cannot read with certainty gets
`waiting <decision>: <finding> <file> has a path attribute the rename gate cannot read`
and stays out of the ticket, busy or not: the gate refuses an edit to that file,
so a person renames the module. A queued ticket that holds such a rename is dropped
with `dropped <id>: <file> has a path attribute the rename gate cannot read`. A
`path` string with an escape or a raw string (`#[path = "pro\u{62}e.rs"]`) can name
the module without its name, so the planner also reads every file that has one, and
such a file counts as one it cannot read with certainty.
A finding on a statement line follows its moved function only when its text is on
exactly one body line there, and that line is a statement of the function. Refill
builds no ticket on a file that a commit on the unit base, not in the synced tip,
changed: it prints
`waiting <decision>: <file> changed in <commit>, not in the synced tip <tip>` or
`waiting for sync: <kind> <crate> [<rule>]: <file> changed in ...`, and waits for the sync. A
decision whose last finding the step built closes. A decision whose answer
changes during the refill gets `waiting <decision>: no longer approved`. A decision
that the step skips gets `waiting <decision>: skipped by the declaration step`,
and refill writes none of its findings. For a finding
that the step cannot read, refill waives it when its file is not in the synced
commit, records the actor `ledger`, and prints the file and the commit. It also
waives a finding whose one-line site text left its file and moved into exactly one file that gained the line since the finding was
judged, records the actor `ledger`, and prints both files and the commit. When
no finding of the decision is left to build, refill closes it. Refill does not assign a queued proposal ticket that was
made before decisions. Drop that ticket with `style_ledger.py ticket drop`,
then run migrate.

A finding is built only when its change is in the code. A rename finding
whose `before` and `after` are one `<parent>::<name>` pair has an applied check
in the finding's file. It is not applied while the old name has a declaration
under its parent, and applied when only the new name has one. A parent is an
enum, struct, union, trait, impl or inline module, or the file itself as its
module (`crate` for `lib.rs` and `main.rs`). The gate refuses an approved ticket
while any such rename is not applied in the worktree, with one line per rename:
`review: <finding id>: <before> -> <after> not applied in <path>`.
`ticket merge` marks a finding built only when its check passes at the
ticket's checkpoint, and prints one line for each other finding. A rename that
is not applied stays filed with no build ticket, so the next refill takes it
into a new ticket: `not built <finding id>: rename not applied in <path>`.
A finding with no applied check stays filed and keeps its merged ticket, so
refill skips it until a person marks it built:
`not built <finding id>: <kind> has no applied check; mark it built with style_ledger.py held built --repo <checkout> --id <finding id> after checking`.
`held built` refuses a finding with no merged build ticket, and a rename that is
not applied at that ticket's checkpoint.
When a plain merge, not a ticket, put the change in the code, run
`style_ledger.py held built --repo <checkout> --id <finding id> [--id <finding id> ...] --commit <merge> [--by <who>]`.
The id can be a unique prefix. The merge must be the synced tip or an ancestor
of it. Each finding must be filed, and no open ticket may build it. The applied
check runs at the merge. A rename that is not applied is refused. A finding with
no check (a rename the check cannot prove, a type or restructure finding) needs
`--by`, and the row records the person as reopen does. A proven rename clears
that record. It prints `built <finding id> at <merge>` for each finding, with
`, read by <who>` when a person read it. A refusal writes nothing for any id.
Without `--commit`, `held built` takes one `--id` and refuses `--by`.
A rename also has no check when its file declares neither name under one
parent, or when more than one item in the file has the parent's name.

To undo a waive, run
`style_proposals.py unwaive --repo <checkout> --proposals <folder> --id <finding id> [--id <finding id> ...] --by <who>`.
The id can be a unique prefix. Each finding goes back to unfiled, with no
decision and no build ticket, so the next `file` puts it in a decision again.
It prints `unwaived <finding id> by <who>` for each finding. The row records the
person in its `unwaived_by` column; `held list` shows `unwaived by <who>` after
the state (`unwaived_by` with `--json`). The record stays after every later
change, a new waive included, so a later `file` never erases it. A second
unwaive records the newer person. When a decline set the cell's newest
judgment to `waived`, unwaive sets it back to `held`.
Unwaive refuses an id that names no finding or more than one, a finding that is
not waived, a finding judged under an older rule text (`file` skips it), a
finding that an open ticket judged or builds, and a blank `--by`. It also
refuses a finding whose decision is declined, because the next `file` waives it
again: answer the decline again first. It strips `--by`. A refusal writes
nothing for any id.

A file rename is a `rename` finding whose `after` starts with `Rename <a> to <b>`,
where `<a>` and `<b>` are sibling `.rs` files and the finding's path ends with `<a>`.
Neither file name may be a Rust keyword, and the rest of `after` may not name a
second `<x>.rs to <y>.rs` pair or a move, split or merge. Only these exact forms
negate a move, so it is not a second change:
- `does not`, `doesn't`, `cannot`, `never` or `without` right before it
  (`does not move`, `without moving`);
- a clause that is `nothing`, `none`, or `no`, at most three words and a noun that
  names code (`definition`, `item`, `type`, `helper`, …) before it, with a place
  word (`into`, `out`, `from`, `to`, …) or the sentence end (`.`, `;`, end of text) after it (`no existing
  definition moves from those callers`);
- a clause of `no` and at most three words before a move word that is a noun, with
  a form of `be` after it (`no additional split is introduced`);
- `no`, at most three words and a noun that names code right after it, then the
  sentence end (`move no skipped definitions.`).

`no longer`, `no more`, `no less`, `no fewer`, `no later`, `no sooner`, `no doubt`
and `no matter` never negate.

Every other form counts as a move, and a person decides: `Do not introduce a
facade or move any definition`, `moves no item into a file`, `move no other helpers
than LockError` and `move nothing apart from LockError` are refused. A clause starts after a mark
(`. ; : , ( )`) or after `and`, `but` or `then`. A hyphenated word such as
`finding-merging` is a name, not a merge.
It is applied when only the new file is in the code, and not applied when only the
old file is. It has no check when a folder of child modules (`lock/` or
`mutation_lock/`) is there; the gate refuses changes in that folder, and a person
moves it. The gate allows the old file to go and the new file to come, and it
compares the new file with the old file at HEAD. The old file must be gone, and
no file may be at the new path in HEAD. The new file may change only as the other
Rust files below may: its own paths such as `crate::ledger::lock::LockError` take
the new name, and its `use` and `mod` lines may take the order rustfmt gives them.
A definition that comes into or goes out of the file, or any other change, is
refused (`<new> (renamed from <old>: change is not only the rename)`). When
another finding of the ticket has the old or the new path, the new file is free,
as a finding's file is: it moves under that finding's approval, so an item rename
declared there can change it. A new file that is byte-identical to the old one
passes with no scan, also outside every crate root (`tests/helper.rs`). The text rules do not catch every move request
(`only LockError goes into ledger/error.rs`): when the worker builds only the
rename, the gate marks the finding built. In other
Rust files it allows only the `mod <old>;` line and the path
segments that name the module: `super::lock`, `self::lock`, `crate::ledger::lock`,
`ledger::lock` from the module that holds `ledger`, `alpha::ledger::lock` in a file
outside the crate (where `alpha` is the library's name), a `pub use lock::...`
re-export, and `lock::` where a `mod` line, import or glob binds the module. A
`#[path = "../src/ledger/lock.rs"]` string changes only its file name, and only when
its path, read from the file's own folder, is the old file; the `mod lock;` line it
marks changes with it. A `mod lock;` line whose `path` string names another file
stays. A file with a `path` attribute the gate cannot read with certainty (one in an
inline module, a `cfg_attr` that holds `path`, or a `path` on an inline module, which
mounts a folder) takes no change at all. A raw name reads as its plain word, so
`mod r#lock;` and `#[r#path = "../src/ledger/lock.rs"]` change as their plain forms do, and
a raw name keeps its `r#` (`mod r#mutation_lock;`). A `mod` or `use` line inside a macro (`stringify!(…)`, a
`macro_rules!` body) stays: there the words are tokens, not items. When the ticket also renames an item, its qualified references there change
too. The `use` and `mod` lines may take the order rustfmt gives them, with their
attributes, and a call's trailing comma may change, except inside a macro or an attribute,
which can match `[1,]` and not `[1]`. The gate refuses every other
change in those files. A comment changes only where a `crate::` path names the
module (`` [`crate::ledger::lock`] ``); a `self::` or `super::` path in a comment
stays. Comments stay in their order. In a block of `use` and `mod` lines, a comment
belongs to the line below it and moves with that line, so a doc comment cannot go to
another module (`/// Alpha API.` stays on `pub mod alpha;`). A comment above the
block that is not a doc comment stays above it, as rustfmt keeps it there.
The decision stays open and approved until every finding is built or waived.

To repair a decision or an old issue that closed too early, run
`style_proposals.py reopen --repo <style checkout> --issue <issue> --parent <slug> [--dry-run]`.
It checks each built rename at the synced tip and sets each one that is not
applied back to filed, with no build ticket. Findings with no check stay built.
A decision opens again with its approval. The findings of an old issue move to
the decision for their rule, crate and rule blob, as migrate moves them; a new
decision takes the old issue's approval. Reopen prints
`filed again <finding id> in <decision>: <before> -> <after> not applied in <path>`
for each finding it files again. For each built rename with no check it prints
`cannot check <finding id> in <path>: <before> -> <after>; pass --finding <finding id> or --applied <finding id>`
and exits 1. With none of these lines it prints `nothing to reopen: every built finding is applied`,
or `nothing to reopen: every checked finding is applied; restructure not checked <count>`
when the issue holds a built restructure (or type) finding. The check never judges
one, so reopen prints no other line for it; `--all` counts them in its totals.
A person who reads the code gives the verdict with `--by <who>`:
`--finding <finding id>` when the change is not in the code, `--applied <finding id>`
when it is. Repeat either option for more findings; the id can be a unique prefix.
Reopen files each `--finding` finding again with no check, in the same run as the
findings the check finds not applied, and prints
`filed again <finding id> in <decision>: <before> -> <after> in <path> by <who>: a person read the code`.
Each `--applied` finding stays built, and reopen prints
`applied <finding id> in <issue>: <before> -> <after> in <path> by <who>: a person read the code`.
The held finding records the person in its `person_read_by` column; the state
shows the verdict. `style_ledger.py held list` shows
`filed again by <who>: a person read the code` for a filed row and
`applied, read by <who>: a person read the code` for a built row (the `reading`
field with `--json`). The record stays after later changes of the finding, except
a new build: when a ticket merge or `held built` marks the finding built again,
the record is cleared, because the person read the old build. A built
finding that a person read is not checked again: reopen does not file it again and
prints no `cannot check` line for it. `--finding` and `--applied` need `--by` and
`--issue`; they are refused with `--all`, and `--by` alone is refused. Reopen strips
`--by` before it stores it.
The dry run prints the same lines and writes nothing. Reopen refuses an issue
that is not approved, an issue that an open ticket builds, an issue with a
merged ticket whose checkpoint the synced tip does not hold yet (sync first), an
old issue that migrate would refuse or whose decision is declined or has no
answer, a blank `--finding` or `--applied`, one that is not one built finding of
the issue, one finding named by both, and an `--applied` rename that the check
finds not applied (the old name is still declared):
`<issue>: finding <finding id>: the check finds the rename not applied; use --finding`.
A refusal writes nothing.
`style_proposals.py reopen --repo <style checkout> --all [--parent <slug>] [--dry-run]`
runs reopen on every approved decision and old issue that holds a built finding.
An old issue takes the parent that its own `Parent:` line names. Only an old
issue with no `Parent:` line takes `--parent`, and with no `--parent` it prints
`skipped <issue>: no Parent line; pass --parent`. A decision that a finding moves
into keeps its own parent; only a new decision takes the old issue's parent. Each
issue with a built finding that the run leaves out or reopen refuses prints
`skipped <issue>: <reason>`, and the others go on. The reasons include
`missing file`, `not approved` and `closed without answer`. It prints the
`cannot check` line for each built rename with no check. The run ends with
`totals: reopen <issues>, filed again <findings>, cannot check <findings>, restructure not checked <findings>, skipped <issues>`
and exits 1 when it skipped an issue or cannot check a rename. The restructure
count never sets the exit code.
`style_ledger.py held add` on a built finding that no person read and whose rename is not at the synced tip
prints `built, but not in the code at <tip>: run style_proposals.py reopen --all (issue <issue>)`
to stderr and changes nothing.

After each merge and at startup, run `style_proposals.py complete --repo
<style checkout>`. It closes only approved decisions, and only when all their
findings are built or waived. It also closes an approved decision with no merged
ticket when no finding is left to build. Decline only with `style_proposals.py decline --repo <style checkout>
--issue <issue> --words '<the user's words>'`. It stores the answer and waives
the findings still filed to the decision with the person actor `user`. Built
findings stay built. Later findings are waived with the same words. Changed inputs stay stale.
A general reason becomes a repo-local rule through `/local_style`.

Record off-rule ideas as candidates, never held proposals. The showrunner
reads `style_proposals.py candidates --repo <style checkout>` and makes a rule
through `/new_style`, or drops the idea with `style_proposals.py drop-candidate
--repo <style checkout> --idea "<text>"`. Use `style_proposals.py sites --repo
<style checkout> --issue <issue>` for a decision's full findings. Review
`style_proposals.py migrate --repo <style checkout> --proposals <folder>
--parent <slug> --dry-run` before migrating old style issues. Migration moves
only the findings filed to each old issue. A moved finding takes the answer of
its decision: a declined decision waives it with the stored words, and a
closed decision opens again by the rule for a later finding that `file` files
(above). A closed decision that refuses the finding stops the migration before
any write, with `style_proposals: <decision>: <reason>`. Migration leaves an
issue with an unfinished build ticket unchanged, and it can resume after a crash.
Migration does not change an off-rule issue that a style unit filed through
`/issue`; it lists the issue as `skipped: off-rule <path>` and counts it in its totals.

A bare duplicate is a filed or built rename finding whose `before` has no `::`,
and whose `before` and `after` are the last `::` segments of a qualified rename
finding with the same rule and file (a `#uncovered` component counts as its
rule). The qualified finding is filed or built, and its `before` and `after` are
one approved `<parent>::<name>` pair: the same parent with a changed last name.
Migrate and reopen never move a bare duplicate into a decision that holds, or
receives in the same run, its qualified finding. They withdraw it into that
decision and print `drop <finding id> in <decision>: duplicate of <finding id>`.
The row records the finding it duplicates. `style_proposals.py drop-duplicates
--repo <style checkout> [--dry-run]` drops each bare duplicate of every open
decision and regenerates the decision's body with its own `Parent:` line. A
decision whose duplicate an open ticket builds, or that has no generated body,
prints `skipped <issue>: <reason>` and is not written. The run ends with
`totals: drop <findings>, skipped <decisions>` and exits 1 when it skipped a
decision. The dry run prints the same lines and writes nothing. `style_ledger.py
held list` shows `duplicate of <finding id>`, `judgment <row> exception` or
`judged again in judgment <row>` after the state of a withdrawn row; `--json` gives `duplicate_of` and `withdrawn_by`.

## Lifetime

The production and its unit plans are standing. An empty queue leaves a unit
blocked, waiting for sync; it never closes its run. A parent's wrap detaches the
style production. While a root, it merges its own hand-offs into the branch
point using `**Parent production:**` as the merge context. Pending hand-offs
survive detachment. The parent detaches a running style child and tells it:
`<parent> wrapped, so you are a root now: run style_sync.py resume and merge your
own hand-offs (docs/style_production.md, Merging back).` Product children still
hold the wrap. The style checkout, unit worktrees with their own plan copies
and pinned rule files, and ledger remain in place. A later `/showrunner:add_style`
from a new parent attaches through `resume`, which handles pending hand-offs.

The sync target at `<main git dir>/style-sync-target` belongs to the production-wide
scratch task `run:<repo>-style`, using the main repository's basename. This is the
standing production's task, not a unit's phase or run. When the style production
itself wraps, after its sync processes finish, run
`~/.claude/scripts/lib/py ~/.claude/scripts/lib/scratch.py end --task run:<repo>-style`
to remove the target and its hold. A parent's wrap keeps this target with the
detached style production.

## Report

Run `/showrunner:style_report [rule|crate]` from the style showrunner or any
showrunner above it. It prints coverage at the synced tip, held changes, pending
hand-offs and changes since the last report. Rule views include open tickets;
crate views include the oldest open ticket. Proposals are shown only when asked.

The script is `scripts/style/coverage_report.py --repo <style checkout>
[--rule <id or unique stem> | --crate <directory or package name>]`. It reads
`style_ledger.Ledger`. It reads `rule_counts(WholeRepository())` once, and its
header uses `summary_of` over those rows, so the header and the table share one count. Unclassified rules are listed
as a count with `pull nate_style`, and awaiting-merge cells count separately
from current coverage. The comparison snapshot is `last_report.json` beside
the ledger, including under `STYLE_LEDGER_STATE_DIR`.

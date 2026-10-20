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
| `names-unit` | dont-repeat-type-name-in-fields, dont-repeat-enum-domain-in-variant-names, avoid-repeated-field-affixes, forbidden-words | spell-out-names, name-bindings-to-match-parameters, prefer-type-named-fields-and-bindings, comments-name-code-constructs | — |
| `values-unit` | never-prefix-unused-fields-or-variables-with, import-constants-at-the-top, derive-test-values-from-production-constants, test-module-allow-boilerplate, agent-must-review-allows, always-use-nextest | no-magic-values, constants-file-organization, prefer-functional-patterns, tests-live-with-the-type-under-test, prefer-observers-over-polling, system-params-carry-their-behavior | — |
| `types-unit` | enums-over-bool-for-owned-booleans, if-else-chains-signal-missing-types, dont-create-traits-for-single-implementations, prefer-from-impl-over-named-constructors, public-failures-return-named-errors, bevy-plugin-ownership, bevy-reflection-registration, extract-observer-run-conditions, prefer-events-over-messages, use-bevy_kana-in-all-bevy-crates | make-invalid-states-unrepresentable, uncovered components of its partial B rules | — |
| `modules-unit` | — | — | module-roots-as-table-of-contents, name-submodules-after-anchor-types, workspace-dependencies, module-structure, when-to-split-a-module, split-by-type-ownership, types-live-with-their-behavior |

Every partial B rule also has a C read in its own family.

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
`git merge-base --is-ancestor HEAD refs/heads/<style merge branch>`, then run
`style_ledger.py ticket checkpoint --repo <unit-worktree> --id <ticket> --commit <HEAD>`
and `style_ledger.py ticket merge --repo <unit-worktree> --id <ticket>`. Act on
its `not built` lines as after a showrunner's `ticket merge` (**Merging back**). Follow
the checkpoint procedure's atomic committed-awaiting-release record using that
ancestry-checked HEAD and release confirmation. Mark the unit's phase done,
report `From <unit>: ticket <id> closed with no code change; <n> held.` and
refill at once. If ancestry fails, leave the ticket assigned and report it.
The gate and `ticket checkpoint` refuse any ticket with owned cells unjudged and name each one.

Refill only the unit worktree's own plan copy:

```bash
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py next-phase --unit <unit> --plan <unit-worktree>/docs/style-production/units/<unit>.md --repo <checkout>
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

The ticket stores its rule text. Workers read the pinned files under the unit
worktree's `docs/style-production/rules/`, including after an interruption;
later rule edits cannot change an assigned ticket's Work Order. A queued ticket
is recovered under the same id if its files remain free; otherwise it is dropped
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
Only named units get Units rows; all four standing plans are written. A unit
without a scope covers every crate. Package names map to workspace member
directories through `rules.workspace_members`.

The plan's Delegation Context carries `- **Pilot scope:** cargo-handler, cargo-port`
below its Family snapshot. Before first launch, edit the showrunner copy;
an interrupted bootstrap rewrites only unlaunched units' rows and scope lines.
After launch, only the unit's own copy counts:
`<main checkout parent>/<slug>-<family>/docs/style-production/units/<family>-unit.md`.
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
must give `--input-scope file` or `--input-scope rule`. The manifest scope covers the workspace root and member
manifests; crate `.` covers only its own files. Exceptions expire with their
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
`seen` skips the following five steps:

1. Merge the new tip into the style merge branch and run its Merge tests.
2. Run `~/.claude/scripts/lib/py ~/.claude/scripts/style/style_sync.py lint --repo <checkout> --checkout <style checkout> --tip <branch-point tip>`.
   This uses `bash ~/.claude/scripts/delegate/verify.sh lint --workspace --no-cache --check`
   at the exact requested commit in a disposable checkout, with workspace mend,
   rustfmt and clippy (`--all-targets --all-features`, workspace lint config).
   Nothing rewrites the tree: mend runs in report mode and rustfmt with `--check`.
   It records per-file diagnostics once per tool; disabled or incomplete checks
   grant no successful coverage. Exit 0 means clean, every tool recorded passing;
   continue the sync. Exit 1 means diagnostics recorded or a tool did not pass;
   continue to `set-tip` and `tip done`. Exit 2 means an error (refusal, failed
   `lint-record` or unreadable checkout); stop the sync.
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
that clears its berth protection. Use the ordinary
`merge_checkpoint.py --production <style doc> <unit> <phase> <hash> --started …`
with one `--also <path>` for each entry of the unit's open ticket's `files`
(`style_ledger.py ticket open --repo <checkout> --unit <unit>`). Its Owns is
`none — takes what the board leaves free`; paths outside that ticket stay refused.

After the merge, run `style_ledger.py ticket merge --repo <checkout> --id <ticket>`
to wake every unit waiting for sync, and send the owning unit director
`From the showrunner of <style slug>: ticket <id> merged; refill.`
For each `has no applied check` line that merge prints, check the change in the
code, and run the printed `held built` command when the change is there. A rerun
of `ticket merge` on a merged ticket prints these lines again. A finding whose
change is not there waits for a person, who makes the change and
runs that command, or waives the finding with
`style_ledger.py held waive --repo <checkout> --id <finding id>`.
The ledger accepts only a checkpoint contained by its stored style merge branch.
Then run
`style_sync.py send-handoff --repo <checkout> --slug <style slug> --checkpoint <style branch tip>`.
It adds the hand-off, sends
`From the showrunner of <style slug>: hand-off <hash>` to the current parent's
`showrunners.py address <parent>`, then records `handoff send`.
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
ancestry on the branch point is the evidence. The receiver decides delivery
state once. A local merge missing from origin is pushed, notified and promoted
before acknowledgment, without merging again. A checkpoint already on origin
merges, pushes, notifies and promotes nothing: it prints
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
parent then rerouted. Failed sends leave the row unchanged.
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

An uncertain result goes to review. Every tier D finding and every
`mode: propose` rule is held. Remove non-mechanical findings and all dependent
edits from the applied diff; merge the remainder after its gate passes.
Record each held finding with `style_ledger.py held add` and its cell with
`record --verdict held`.

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
Findings judged under an older rule text stay unfiled.
A `skipped <issue>: <reason>` line means that filing cannot write that
decision. When filing new findings, those findings stay unfiled. When
refreshing an open decision, its findings stay filed and the file stays as it
was. The other decisions still file. When it skips one or more decisions,
`file` exits 1. Fix the named decision file as the reason tells, then run
`file` again. A `skipped <finding>: rule changed since judged` line names one
finding, not a decision, and does not change the exit status.

Filing first withdraws each filed finding whose site has an `exception` as the
newest judgment under its rule's current text, when the finding was judged
under an older text. `withdrawn` is its own state, not `waived`, and the
finding records that judgment's row. A finding that an open ticket builds is
kept. The judgment must also be newer than the finding's own judgment. A
decision whose findings are all withdrawn closes with a reason that names them
and the judgment rows. A decision that keeps other findings shows only those,
and `complete` closes it when they are built or waived.
`style_proposals.py withdraw --repo <style checkout> --proposals <folder>
[--dry-run]` runs this step alone. It prints
`withdraw <finding> <rule> <path>:<line>: judgment <row> exception under <blob>`,
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
- Another filed finding whose file is still there stays filed for a person. A
  person runs `style_ledger.py held waive --repo <checkout> --id <finding>`, or
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
them with its stored words, and `file` prints
`waived <n> findings in <decision>: declined: <words>`. A new rule blob needs a new
answer. Each decision shows counts and up to three examples from different
files. The ledger holds its full site list. Show decisions only when the user
asks through `/showrunner:proposals` or `/adhoc_review`.

Approve with `proposal_issue.py approve <issue> --showrunner <parent>
--unit <unit> --plan <unit's own plan copy>`. Do not add phase numbers.
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
`waiting <decision>: <finding> declared in another crate: <file>` or
`waiting <decision>: <finding> declaration gone`, and stays out of the ticket. A
decision whose last finding the step built closes. A decision whose answer
changes during the refill gets `waiting <decision>: no longer approved`. A decision
that the step skips gets `waiting <decision>: skipped by the declaration step`,
and refill writes none of its findings. For a finding
that the step cannot read, refill waives it when its file is not in the synced
commit, and prints the file and the commit. It also waives a finding whose one-line site text left its
file and moved into exactly one file that gained the line since the finding was
judged, and prints both files and the commit. When no finding of the decision
is left to build, refill closes it. Refill does not assign a queued proposal ticket that was
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
second `<x>.rs to <y>.rs` pair or a move, split or merge.
It is applied when only the new file is in the code, and not applied when only the
old file is. It has no check when a folder of child modules (`lock/` or
`mutation_lock/`) is there; the gate refuses changes in that folder, and a person
moves it. The gate allows the old file to go and the new file to come, with any
content. In other Rust files it allows only the `mod <old>;` line and the path
segments that name the module: `super::lock`, `self::lock`, `crate::ledger::lock`,
`ledger::lock` from the module that holds `ledger`, `alpha::ledger::lock` in a file
outside the crate (where `alpha` is the library's name), a `pub use lock::...`
re-export, and `lock::` where a `mod` line, import or glob binds the module. When
the ticket also renames an item, its qualified references there change too. The
`use` and `mod` lines may take the order rustfmt gives them, and a call's trailing
comma may change. The gate refuses every other change in those files. Comments stay
in their order; only comments inside a block of `use` and `mod` lines move with them.
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
the findings still filed to the decision. Built findings stay built. Later
findings are waived with the same words. Changed inputs stay stale.
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
decision closed as built opens again with its approval. A decision closed with
nothing left to build also opens again with its approval for a later finding. Migration leaves an
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
held list` shows `duplicate of <finding id>` or `judgment <row> exception` after
the state of a withdrawn row; `--json` gives `duplicate_of` and `withdrawn_by`.

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

## Report

Run `/showrunner:style_report [rule|crate]` from the style showrunner or any
showrunner above it. It prints coverage at the synced tip, held changes, pending
hand-offs and changes since the last report. Rule views include open tickets;
crate views include the oldest open ticket. Proposals are shown only when asked.

The script is `scripts/style/coverage_report.py --repo <style checkout>
[--rule <id or unique stem> | --crate <directory or package name>]`. It reads
`style_ledger.Ledger`; its header uses `summary()`. Unclassified rules are listed
as a count with `pull nate_style`, and awaiting-merge cells count separately
from current coverage. The comparison snapshot is `last_report.json` beside
the ledger, including under `STYLE_LEDGER_STATE_DIR`.

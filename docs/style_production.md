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
and `style_ledger.py ticket merge --repo <unit-worktree> --id <ticket>`. Follow
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
in-scope crates is blocked by a structure cell on its file or crate, until the
modules unit's structure tickets merge
(an unclassified catalog also says `pull nate_style`); 4 means
`board unavailable: <reason>`; 5 means
`waiting for merge: <ticket> checkpointed at <commit7>`. Unknown reservation
availability stops selection without recording work. `queue` is a read-only
preview and also returns 4 when the board is unavailable.

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
--style-branch <style branch>` and logs its change lines. Reconcile recovers work
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
and append its reconcile change lines to **Log**. Reconcile stores the branch
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

One style decision covers one rule and crate under one rule blob. An answer
also covers later findings under that rule text. A new rule blob needs a new
answer. Each decision shows counts and up to three examples from different
files. The ledger holds its full site list. Show decisions only when the user
asks through `/showrunner:proposals` or `/adhoc_review`.

Approve with `proposal_issue.py approve <issue> --showrunner <parent>
--unit <unit> --plan <unit's own plan copy>`. Do not add phase numbers.
Refill builds approved findings in small tickets before coverage work.
Each ticket permits its own approved findings and proven rename references.
Other findings stay held. Package checks still run. The parent schedules
changes to a live unit's files through `produce.md`'s `<CrossUnitChange>`.
Refill waives a finding whose file is not in the synced commit, and prints the
file and the commit. When no finding of the decision is left to build, refill
closes it. Refill does not assign a queued proposal ticket that was
made before decisions. Drop that ticket with `style_ledger.py ticket drop`,
then run migrate.

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

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
| D | One check per crate, including its listed sites and uncovered read. |
| E | Governs how fixes are made; never swept or counted as coverage. |

## Families

One unit director per family applies an exception decided in one crate the same
way in every crate. Use `rules.py list` for the current classified rules and
canonical ids; repository-local rules join their declared family.

| Unit | B: listed rules | C: file reads | D: crate checks |
| --- | --- | --- | --- |
| `names-unit` | dont-repeat-type-name-in-fields, dont-repeat-enum-domain-in-variant-names, avoid-repeated-field-affixes, forbidden-words | spell-out-names, name-bindings-to-match-parameters, prefer-type-named-fields-and-bindings, comments-name-code-constructs | — |
| `values-unit` | never-prefix-unused-fields-or-variables-with, import-constants-at-the-top, derive-test-values-from-production-constants, test-module-allow-boilerplate, agent-must-review-allows, always-use-nextest | no-magic-values, constants-file-organization, prefer-functional-patterns, tests-live-with-the-type-under-test, prefer-observers-over-polling, system-params-carry-their-behavior | — |
| `types-unit` | enums-over-bool-for-owned-booleans, if-else-chains-signal-missing-types, dont-create-traits-for-single-implementations, prefer-from-impl-over-named-constructors, public-failures-return-named-errors, bevy-plugin-ownership, bevy-reflection-registration, extract-observer-run-conditions, prefer-events-over-messages, use-bevy_kana-in-all-bevy-crates | Uncovered components of its partial B rules | — |
| `modules-unit` | — | — | module-roots-as-table-of-contents, name-submodules-after-anchor-types, workspace-dependencies, module-structure, when-to-split-a-module, split-by-type-ownership, types-live-with-their-behavior |

Every partial B rule also has a C read in its own family.

## Ticket queue

One ticket per phase:

- **Read:** one family, about 5,000 lines in one module subtree, 10–25 files.
- **Listed:** one rule and crate; pack whole files in path order into tickets of
  at most 150 sites. A file with more than 150 sites gets one ticket by itself.
  Never split a file's sites between tickets.
- **Structure:** one crate.

Run tier D first. A repair phase always precedes a new ticket. After an
interruption, resume the assigned or checkpointed ticket from
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
Report a waiting state once and end the turn blocked until sync, a board retry
message, or `ticket <id> merged; refill.` wakes the director.

A ticket with no code changes makes no commit: record its cells at HEAD, verify
`git merge-base --is-ancestor HEAD refs/heads/<style merge branch>`, then run
`style_ledger.py ticket checkpoint --repo <unit-worktree> --id <ticket> --commit <HEAD>`
and `style_ledger.py ticket merge --repo <unit-worktree> --id <ticket>`. Follow
the checkpoint procedure's atomic committed-awaiting-release record using that
ancestry-checked HEAD and release confirmation. Mark the unit's phase done,
report `From <unit>: ticket <id> closed with no code change; <n> held.` and
refill at once. If ancestry fails, leave the ticket assigned and report it.

Refill only the unit worktree's own plan copy:

```bash
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py next-phase --unit <unit> --plan <unit-worktree>/docs/style-production/units/<unit>.md --repo <checkout>
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py queue --repo <checkout> [--unit <unit>]
```

`next-phase` returns 0 after writing a phase, resuming an assigned ticket, or
reporting `repair first`; 3 means `waiting for sync` (an unclassified catalog
also says `pull nate_style`); 4 means `board unavailable: <reason>`; 5 means
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
ledger ticket's `base` field. The footprint gate uses that literal commit id as
`--base` and `--worktree`, including after an interruption. The gate judges
the uncommitted tree the checkpoint will stage, using a temporary index; it
never changes the real index. No private Git ref
keeps a dropped ticket's abandoned commits reachable.

## Ledger

One style production and one synced branch point per repository. The ledger is
`~/.local/state/style/<repo>/ledger.sqlite`; only `style_ledger.py` writes it.
Startup runs `style_ledger.py reconcile --repo <checkout> --branch-point <branch>
--style-branch <style branch>` and logs its change lines. Reconcile recovers work
state from Git without advancing an existing synced tip.

Rule ids are `global:rust/<rule>.md` and
`local:docs/<member>/style/<rule>.md`. A member-local rule governs its member
crate; a root `docs/style` rule governs the repository. Unclassified rules have
no cells or tickets; show their count with the action **pull nate_style**.

The Rust cell universe contains tracked, included non-vendor `.rs` files.
Listed rules also gain cells for tracked non-Rust files holding their sites;
those cells use file fingerprints regardless of the rule's input scope.
Tier D has crate cells. Sources outside workspace members belong to crate `.`.

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
`manifest` scope. The manifest scope covers the workspace root and member
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
has no uncovered component. A partial D rule's crate cell covers both reads.

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
that clears its berth protection. Then send the style branch checkpoint as a
hand-off to the parent. Style work does not wait for a sync to go home.
The child hand-off phase supplies the merge and acknowledgment contract;
the ledger marks a hand-off merged only when the branch point contains it.

## Yielding

Selection skips files reserved on the berth board, changed on live units'
unmerged branches, or dirty in their worktrees. Acquire reservations when
editing begins. Product work always wins a conflict.

After a clean incoming merge mid-ticket, recheck the file's incoming diff before
checkpointing and record the new fingerprint. On a conflict or berth overlap,
drop every finding touching that file, including all its edits in other files;
a multi-file rename is dropped as a whole. Release reservations no remaining
finding touches, rerun the gate, and leave dropped cells stale for later work.
Style units obey build holds and memory admission like any unit.

## Big changes

Apply a fix without asking only when all mechanical criteria hold:

1. No file is added, deleted, renamed or moved, and no `mod` line changes.
2. No item reachable from another crate changes name, signature, fields or variants.
3. No type gains or loses a field, variant, generic parameter or trait impl,
   and no new type, trait or error type appears.
4. A rename's sites are in one crate and at most 10 files.
5. The finding's diff is under 200 changed lines.
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
for anything the filter cannot clear. Without `--finding-map`, the whole diff
is one finding. An optional JSON map assigns finding ids to file lists, or to
objects with `files` and `hunks` lists. A hunk is identified by its `path`,
`old_start` and `new_start` from a zero-context diff; every changed hunk must
have one owner. The gate folds merged-in parents into the comparison base so
their changes are not judged as ticket work. A conflicting fold requires review.
Each finding must preserve behavior using only its own edits; another finding's
call-site changes cannot complete its rename. Changed documented usage, including
block documentation, requires review. Glob imports prevent proving a rename's
identity, and public glob re-exports prevent establishing module privacy.
Generic signature brackets and attributes on unrelated items alone do not
require review. Derive changes count as trait implementation changes.
Changes inside macro invocations require review even when only spacing changes;
type-shape checks include unions and their derives.

File held changes as issues tagged `proposal` and `style` under the production's
required `**Proposals:**` folder: one issue per rule and crate for renames or
type changes, one per crate for restructuring. Include sites, before and after,
live units holding those files, and a recommended answer. Show the count in
reports; present proposals only when asked through `/showrunner:proposals` or
`/adhoc_review`. The proposal phase supplies filing and approval commands.

## Report

Use `/showrunner:style_report` for coverage, held changes and pending hand-offs.
The report phase supplies the command and its output contract.

## Lifetime

The production and its unit plans are standing. An empty queue leaves a unit
blocked, waiting for sync; it never closes its run. A parent's wrap detaches the
style production. While a root, it merges its own hand-offs into the branch
point using `**Parent production:**` as the merge context. Pending hand-offs
survive detachment; the hand-off and proposal phases supply the root merge and
detach procedures.

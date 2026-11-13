# Refill in seconds

Status: accepted design (style-unit, 2026-11-12). Phase 1 is next.

The source review uses checkout `5eb40d6403040cc9ffe9b8eaa849c4758756fbfc`.
The measurements come from the design brief dated 2026-11-12.
This design used no live ledger, refill run, or memory probe on natedev.

## Result

A refill should read a maintained selection index and update facts whose inputs changed.
It should not reconstruct the repository and ledger history for each decision, family, or selection pass.

Keep the ledger as the source of judgments, findings, tickets, and sync state.
Store derived facts and their dependencies in new SQLite tables in the same database.
Keep immutable source facts by Git object identity.
Find changed ledger rows through table counters and row versions.
Hash mutable files at each existing file-read stage.
Use the existing selection policies over indexed rows.

The target is a few seconds and less than 200 MB peak RSS on natedev for an ordinary cargo-liner refill.
Use five seconds as the measurement target for representative small changes.
This target adds no runtime deadline or memory limit.

One changed input can affect many results.
A crate-scoped judgment can become stale after one file changes.
A new rule can require analysis of its full reach.
Printing existing waits costs at least the size of that output.
The cost model therefore includes affected dependencies and required output, not just the number of edited files.

Initial construction requires a separate measurement.
It can exceed the ordinary refill target.
It does not belong in every refill.

## Round 2 findings

| Finding | Change |
| --- | --- |
| D1 | Remove the filesystem observer; hash the small mutable roots and cache parsed facts by content hash |
| D2 | Use per-table counters and latest row versions, including deletion markers; remove the universal event journal |
| D3 | Ship selection, candidates, judgments, and sites in descending measured cost; state cumulative estimates for each phase |
| D4 | Begin with one restored Mac input set; add differential cases with the phase that changes their inputs |
| D5 | Specify eleven tables and three runtime components; merge facts with shared keys and state a five-line debugging procedure |

## What the source shows

The current entry is `next_phase`, not `_next_phase`.
The current coverage helpers are `_refill_cells` and `_tickets_from`, not `ranked_candidates`.
The brief describes the same repeated work under earlier names.

| Current work | Cause of repeated cost | Replacement |
| --- | --- | --- |
| `Ledger._refresh_sites` | Any marker mismatch exports every blob and runs every listed rule | Candidate facts by input partition |
| `Ledger._coverage` | Each call enumerates every cell and evaluates its judgment history | Indexed current cell states |
| `_judgment_inputs_match` | Historical trees and source tokens accumulate during repeated comparisons | Stored scope facts and comparison results |
| `_refresh_site_rows` | Any database change reloads all finding histories and active judgments | Changes by finding, ticket, rule, and cell |
| `_built_renames` | Each rebuild reads all built findings and historical trait positions | Stored rename edges and dependency queries |
| `held_findings` | It selects ids, then calls `_held` for each id | Indexed joins with one row decoder |
| `_select_phase` | Own, count, borrow, and second-pass selection repeat source work | Shared facts with explicit observation stages |
| `read_slices`, `fill_decision_batch` | They read source through separate Git processes for line counts | Stored line counts by blob |
| `EditedFiles` | Each process repeats searches and declaration scans | Persistent reference and module indexes |
| `complete`, `run_follow`, `renumber` | They revisit completed or followed decisions and the issue collection | Indexed completion and publication inputs |

The design follows the code where the as-built document differs.
For example, current rule comparison uses validated judgment identities and rule generations.
Raw rule-blob equality alone does not describe current coverage.

## Tables, components, and identity

The complete design adds eleven persistent tables in the existing ledger database.
Each phase adds only the tables and columns that its improvement needs.
Existing source tables remain authoritative.

| New table | Key | Stored facts and lifetime |
| --- | --- | --- |
| `refill_tables` | Source table name | Current change counter and last applied counter; persistent |
| `refill_rows` | Source table, source primary key | Latest change version and present/deleted state; one persistent record per source key |
| `refill_files` | Source root, path | Content hash, parser version, and parsed fields, including rule metadata; replace after a content or membership change |
| `refill_objects` | Object store, object id, reader version, fact kind/key | Typed blob, tree, or commit facts; immutable |
| `refill_inputs` | Input kind, exact input identities, comparator version | Scope fingerprints, input comparisons, and history-range results; immutable for those inputs |
| `refill_candidates` | Rule source/id, generator partition | Input identity, generator version, ordered sites, and explicit empty results; replace when dependencies change |
| `refill_cells` | Catalog source, rule component, path, view | Current state, verdict, relevant judgment ids, held exclusion, and dependencies; replace affected rows |
| `refill_findings` | Finding id, view | Finding revision, anchor, site proof, declaration outcome, rename endpoints, footprint, and rank exemption; replace affected rows |
| `refill_groups` | Group kind, owner, view, group key | Ordered candidates, counts, waits, ticket footprints, or decision summaries; replace affected groups |
| `refill_outputs` | Output root, path | Desired bytes, last successful input identity, and unfinished publication work; retain until publication completes |
| `refill_dependencies` | Consumer table/key, source kind/key | Reverse dependency edges, including membership subscriptions; replace with the consumer's facts |

`refill_objects` combines the former object, tree, and commit tables under their common immutable object key.
The object kind selects a typed decoder.
Tree entries, names, and declarations occupy individual typed rows under that object key.
Indexed lookup columns expose those rows without decoding a whole object payload.
`refill_inputs` combines immutable scope results and comparisons under exact input identities.
Current rule metadata belongs in `refill_files`; historical rule-object facts belong in `refill_objects`.

`refill_findings` keeps each finding's derived answers together at the same view.
Built-rename lookups use indexed endpoint fields in those rows.
`refill_groups` stores each group's members, contributions, and summary together.
Its kinds are coverage batch, family count, ticket footprint, decision, publication ranking, and availability.
Existing sort keys remain typed columns, not opaque serialized ordering rules.
`refill_files` describes observed files; `refill_outputs` retains unfinished publication even when the output file disappears.
Those tables have different lifetimes despite their shared path key.

The mutable derived tables hold their own dirty version and last completed input identity.
There is no separate dirty queue, event journal, rename-edge table, or footprint table.
Indexes and SQL views do not add another state owner.

The runtime adds three components:

| Component | Location | Responsibility |
| --- | --- | --- |
| `RefillInputs` | `scripts/style/refill_inputs.py` | Hash mutable files and stream Git objects/history through existing readers |
| `RefillFacts` | `scripts/style/refill_facts.py` | Own the eleven derived tables, update affected facts, and expose indexed queries |
| `RefillContext` | `scripts/style/style_plan.py` | Supply stage-specific facts to the existing selection functions |

`style_ledger.py` retains source mutations and schema migration ownership.
The existing generator, proof, ranking, and rendering functions retain their semantic decisions.
`RefillFacts` uses explicit update functions for each relation, not a general workflow engine.
One comparison script is test support, not a fourth runtime component.
The design adds no background process or independent publication service.

A view names a unit and revision role, with current commit identities stored as inputs.
Advancing that view changes affected rows rather than copying every unchanged row under a new commit key.
Historical object facts remain shared.
An active selection stage retains its observed row values until that stage ends.

Catalog source identifies the actual rule roots and execution checkout.
It does not hash every rule into every cell key.
Scope identities include the exact paths, modes, kinds, and objects that the current comparison uses.
Reader versions identify the relevant parser or policy, not the entire repository HEAD.
Negative results retain dependencies on the domain that could make them positive.
A cached I/O failure never substitutes for a current successful read.

### Debugging one wrong ticket

1. Identify the wrong selection stage and its resolved inputs in `RefillContext`.
2. Compare that stage's ordered `refill_groups` members with the old selector.
3. Inspect the contributing `refill_cells` or `refill_findings` row and its stored explanation.
4. Trace its `refill_dependencies` to changed row versions, file hashes, or Git identities.
5. Confirm the corrected dependency or calculation with that differential case.

## Change discovery

### Ledger changes

Per-table counters plus row versions are sufficient.
No current refill consumer needs an ordered history of cache-invalidating events.
The source ledger already owns the history that changes selection semantics.

Each source mutation increments its table's counter and replaces that key's `refill_rows` entry in the same transaction.
A delete leaves a versioned deletion marker.
A primary-key change records the old key as deleted and the new key as present.
An index on `(table, version)` finds rows changed since that table's applied counter.
The updater reads no unrelated source rows when the counter is unchanged.
An untouched source row has implicit version zero.
Initial projection construction reads that source once without creating version records for every existing row.

Triggers cover every relevant write path, including corrections and direct SQL mutations.
They do not attach to derived tables.

The trigger set covers these source groups:

1. Judgments, active generations, invalidations, and corrections.
2. Held findings, anchors, follow coordinates, dispositions, and withdrawal records.
3. Tickets, proposal links, predecessors, yields, widened paths, and batch ownership.
4. Sync, lint runs, handoffs, reconciliation, and pinned rule texts.
5. Sync waits and other selection-visible state.

Old lookup keys come from the consumer's previous projection and reverse dependencies.
New lookup keys come from the current source row.
For a finding moved from file A to file C, the updater removes its A contribution and adds its C contribution.
An intermediate move through B needs no replay when no published projection observed B.
Deletion uses the previous contribution and the retained deletion marker.
Decision links, ticket claims, and judgment-cell keys use the same rule.

The updater invalidates existing consumers through stable source ids.
It also invalidates membership subscribers for old and new rule, file, decision, or scope keys.
This handles additions that have no previous consumer edge.
It marks affected projections before replacing their previous contributions.

Source reads, version reads, dirty marking, and applied-counter advancement use one consistent SQL transaction.
Expensive calculations use immutable input identities outside that transaction.
Their publication records the input versions they used.
Newer source versions retain dirty work for the next required observation stage.
An interrupted process leaves either the old applied counter or durable dirty rows.
Deletion markers remain available; timestamps never identify source revisions.

This design needs no journal because previous projected keys remain available until invalidation completes.
A future consumer that needs intermediate old/new transitions would require a journal for that consumer alone.
Such a consumer is outside the present design.

Queries retain `(recorded_at DESC, id DESC)` ordering and the current `active_judgments` predicate.
Source indexes cover judgment cells and findings by state, proposal, rule, path, and ticket.

### Git changes

Resolve the synced tip, style tip, pushed main tip, and relevant unit tips at the current observation points.
Compare stored tree nodes with the new roots.
Skip equal subtrees by object id.
Read changed tree nodes and new blobs through streaming Git batch readers.

Store tree nodes once, including modes, symlinks, and gitlinks.
Keep repository inclusion separate from generator inclusion and judgment input membership.
Those sets differ in the existing code.

Maintain reverse membership for files, module scopes, crate scopes, manifests, names, and declaration identities.
A scope subscription includes membership changes.
It cannot consist only of paths that existed when the result was computed.
Adding a file can invalidate a crate judgment, a name search, or a previously absent declaration.

Maintain reachability for commits referenced by active judgments, removals, findings, and tickets.
On a forward tip change, traverse the newly reachable commit range and update affected references.
On a rewind or branch replacement, traverse both sides of the reachability change.
Such a change can affect many judgments without changing a file blob.

Keep endpoint tree differences separate from history-sensitive differences.
`main_changes` must retain touched paths from commits whose changes were later reverted.
`unsynced_changes` must retain its newest-commit order and dense-combined merge behavior.
Cached history results include the exact Git options and both endpoint commits.

Object content is immutable, but object availability can change after pruning or object-store replacement.
The existing Git reader resolves required historical objects through its batch process before reusing their persistent facts.
It retains those availability results only for the current invocation.
Object-store identity remains part of each persistent key.
Retain the current missing-object and read-error behavior instead of using cached bytes as substitute authority.
Rule repositories require the same treatment.

### Mutable files

Issue files and local rule files can change without a ledger write or Git commit.
A tip-keyed cache cannot detect those changes.
Modification time and size alone cannot prove unchanged bytes.

At each existing file-read stage, `RefillInputs` enumerates the required roots and hashes every file's bytes.
It compares the path set and hashes with `refill_files`.
It reparses only new or changed files.
It removes disappeared files and invalidates their previous consumers.
Parsing uses the bytes that supplied the hash, not a separate unchecked read.
Cached fields describe file syntax; the catalog binds rule reach from the current manifest facts.
A manifest change can therefore change reach without reparsing unchanged rule text.
A parser-version change rebuilds that parser's facts once.

The roots include about 1,500 issues, the rule roots, plans, production documents, goals, ranking output, staff data, and configuration.
They also include worktree manifests used by `crate_root`, `crate_library`, and local rule reach.
Those readers retain their current checkout sources.
Atomic replacements and edits that preserve size or modification time still change the content hash.
The existing approval and publication readers retain their required reads at mutation boundaries.

Estimated warm cost is 30–100 ms per file-read stage for these roots.
The estimate assumes roughly 10–30 MiB of source bytes already in the filesystem cache.
Four stages therefore cost about 120–400 ms, including enumeration, reads, and hashing.
These figures are estimates, not measurements from this design task.
The first Mac comparison records actual file count, byte count, stage count, and hashing time.
Cold filesystem reads can cost more and receive a separate timing.

This small repeated read keeps ordinary editor writes visible without a daemon or platform event protocol.
The incremental savings come from avoiding repeated parsing, Git reconstruction, and ledger-history analysis.

### Availability

Read `cargo-berth board --json` within each existing refill lock interval.
The current public interface supplies a complete board.
Its sections contain journal positions, but the current reader provides no delta contract.
This design does not invent a supported `--since` option.

Keep the existing `Board` reader and its envelope parsing semantics.
Store its previous relevant fields in the availability group.
Compare reservation fields with that preceding observation.
Invalidate only matching file and tree scopes when those fields change.
Index file scopes and tree prefixes within `RefillContext`.
Do not materialize every repository path for `tree:.`.

Read live worktree status through the existing Git command.
Persist branch-difference facts by the exact branch and merge-base commits.
Use indexed ticket footprints for other units' claims.
Each existing availability stage reads worktree status again.
Status equality permits reuse of the corresponding derived busy intersections.

The complete board read and initial worktree status reads impose a source-discovery cost.
They do not justify recomputing coverage or finding proofs.
Their cost appears explicitly in the refill budget below.
A strict delta-only total requires delta interfaces from these providers.
That separate interface work is unnecessary unless these reads prevent the measured target.

## Incremental derived facts

### Candidate generators

Replace filesystem traversal with an input interface over indexed paths and immutable blobs.
Keep each generator's current text, ordering, masking, decoding, and path semantics.
Do not replace its logic with a more precise Rust interpretation.

| Generator group | Partition and dependencies |
| --- | --- |
| Allows, fields, enums, observers, literals, struct fields | File blob plus relevant path, spec, and scanner identity |
| `module_root_items`, `submodule_names` | File facts plus exact path and directory name |
| `regex` | File content plus spec, path selection, applicable ignore files, and ripgrep configuration/tool identity |
| `workspace_deps` | Member manifest plus root membership and glob expansion |
| `bevy_kana_usage` | Manifest dependency flags, `src/lib.rs` presence, and each affected source file |
| `single_impl_traits` | Per-file masked facts and repository-wide contributions for each trait name |
| `pre_filter` | File blob, pattern, and membership in the shared listed-file universe |

`single_impl_traits` currently uses a regular expression for each declared trait name.
Retain that exact matcher over stored masked-file facts.
A changed file updates contributions for names that can match its old or new content.
A new trait name requires contributions from every candidate file in its search set.
An index can reduce that search set only when it includes every possible regex match.
Trait-name counts invalidate the corresponding declaration sites, including sites in unchanged files.

The regex generator must preserve ripgrep's file selection and regex semantics.
Do not replace ripgrep patterns with Python patterns.
Use changed-file inputs while retaining the current ignore and path context.
Ignore changes invalidate the affected selection domain, including newly eligible files.

The pre-filter universe is the Rust universe plus paths named by any candidate generator.
Maintain counts for those path contributions.
Adding or removing the last contribution changes every applicable pre-filter partition for that path.
Generator-specific workspace membership remains distinct from `rules.workspace_members` where the existing functions differ.

Join the partition results into ordered listed-site views.
Store empty partitions explicitly.
Ordinary tip advancement needs no repository export and no copy of unchanged listed sites.

### Judgments and coverage

Store the current cell universe as indexed relations.
Apply membership differences when rules, paths, ignores, manifests, or generator sites change.
Preserve tier A, partial B, `#uncovered`, tier C, and tier D behavior.

Update each affected cell from its ordered judgment rows.
Keep the current ordering by `recorded_at DESC, id DESC`.
Keep declared scope selection, explicit reads, removal barriers, active generations, and administrative rule equivalence.
Reverting a rule body must not revive an earlier generation.

Store reusable scope comparisons separately from individual judgments.
Many judgments compare the same crate or module inputs.
Unchanged scope comparisons require no historical source read.

For modules, store the exact `RustCode` representation by blob.
Retain inner-doc positions and the current mode, path-set, file-kind, and tokenization conditions.
Names and values continue to use their current input comparisons.
No shared cache broadens comment-only carry.

Tier A uses indexed lint runs by tool, tree, and configuration identity.
The configuration identity includes the external mend configuration where the current code includes it.
Store aggregate state counts by crate and family for selection ordering.
Update those counts from old and new cell states.

### Findings, declaration follow, and footprints

Load requested findings with joined queries instead of one `_held` query per id.
Keep the existing state decoder and result ordering.
Separate immutable source evidence from approval-dependent declaration outcomes.

Finding dependencies include its anchor, judging ticket, original scope, removal history, pinned rule, and generation.
Declaration outcomes also depend on relevant names, imports, owners, trait identities, and built-rename edges.
Negative searches subscribe to their search domain.
An additional declaration must invalidate a previous unique or absent result.
An approval change can alter a declaration outcome without changing source bytes.

Store built-rename endpoint facts when their finding or history changes.
Query rename chains by crate, name, owner, and trait dependencies.
Retain fork, cycle, ambiguous identity, statement, and open-build outcomes.
Do not rescan all built findings when one judgment arrives.

Build reference and module indexes from each changed blob once.
The name index covers the exact existing word-search behavior, including `.md` references.
The module index retains uncertain `path` strings and conservative owner results.
Each footprint includes the exact ordered union for the synced tip and unit base.
The selected proposal also uses pushed-main facts at the current point in selection.
Ticket footprints include widened paths and the existing pipeline proof.

Busy state does not invalidate a source footprint.
It changes only the indexed availability intersection for that footprint.
This separates source analysis from reservation changes.

### Ranking, batching, and publication

Maintain open-work contributions by `(path, family, contributor)`.
Use contribution counts so removal of one finding cannot remove another finding's rank block.
Apply family pilot scopes through indexed settings relations.
Store rank exemptions with their source and nearby types-finding dependencies.

Maintain decision order by the current creation date and filename ordering.
Maintain finding order by the current creation time and finding id ordering.
Maintain crate ordering from stale counts and modules' structure-blocking counts.
Retain the stable final preference for `first_files`.

Persist line counts using the current decoding and `splitlines()` behavior.
Keep `balanced_slices`, listed-site packing, decision limits, and the oversized-first-file exception unchanged.
Recompute only a grouping whose eligible members, lengths, rules, priority, or busy paths changed.
One changed member can alter every slice in that grouping.
The cost includes that grouping's size; it is not always one-file work.

Cache complete grouping results, not just a previously selected ticket.
The next eligible ticket can change after a reservation or claim changes.
Family borrowing still selects the largest buildable finding count, with the current family-rank tie break.

Index decision completion by unresolved finding counts and linked merged tickets.
Index followed decisions that require publication, including recovery after an interrupted refresh.
Retain `complete` and `run_follow` at their current logical stages.
Their indexed reads replace full finding scans.

`complete` currently invokes renumbering whenever any proposal ticket is merged.
Cache renumbering inputs and derived output through the source catalog.
The input set includes all relevant issues, dependencies, goals, and the ranking export.
An unchanged successful publication requires no issue parsing or full ranking calculation.
Changed ranking inputs update affected dependency order and ranks through the same ranking rules.
A changed rank can require many file writes; that cost belongs to the actual output change.
Retain existing writer locking, error behavior, and interrupted-publication recovery.
Do not remove the renumber call merely because this refill closed no decision.

## One refill, in order

Let `D` be changed source keys and changed Git entries.
Let `A` be affected dependency rows, including changed scope members and affected batching groups.
Let `C` be newly traversed commits and history records.
Let `S` be bytes of newly required Git blobs.
Let `H` be the total mutable-file enumeration and hashing work across file-read stages.
Let `Q` be changed candidate/proof rows and selected ticket data.
Let `W` be emitted diagnostic and phase bytes.
Let `L` be current board and worktree discovery work.
Let `N` be the size of the indexed relations.

The ordinary cost is indexed lookup work plus `O(D + A + C + S + H + Q + W + L)`.
Index updates add the usual `log N` lookup cost.
Some affected algorithms retain their current grouping or sequence-comparison cost.
The budget measures those costs rather than claiming every algorithm is linear.

| Step | Work | Cost |
| --- | --- | --- |
| 1 | Hash required files and resolve unit, plan, catalog sources, and repair state | This stage's `H`, changed-file parsing, and indexed lookups |
| 2 | Read changed row versions and update withdrawal/completion facts | `D`, affected rule pins, findings, and publication dependencies |
| 3 | Preserve discard, assigned-ticket resume, and predecessor handling | Changed or open unit tickets plus required phase output |
| 4 | Resolve revision roles and update source, scope, and history facts | `C + D + S` and affected membership |
| 5 | Enter the existing refill lock and read current availability | `L` plus changed reservation and claim intersections |
| 6 | Update dirty coverage, finding, rank, and batch partitions | `A + Q`, with each new blob parsed once per reader |
| 7 | Run own-family selection in the existing order | Indexed candidates, required effects, and `W` |
| 8 | Run other-family counts and the selected borrow path when needed | Changed eligibility partitions and required borrowed output |
| 9 | Assign the selected ticket and write its phase | Selected data, existing transactional writes, and phase bytes |
| 10 | Preserve the existing sync-wait registration and second pass when needed | Changed versions, fresh hashes and availability, and repeated required output |

Steps with no changed facts use stored results.
Stored versions and content facts remain useful across separate CLI processes.
The cost includes fresh hashing at every required file-read stage.
Moving work into the same process or warming a Python dictionary is insufficient.

### Preserve observation order

A refill is not one pure query.
It withdraws, follows, waives, closes, drops, starts, and assigns work.
Those actions can affect later observations within the same invocation.

Use a `RefillContext` with stage-specific references to derived facts.
Advance it only where the current algorithm observes new state.
Share unchanged immutable facts across all stages.

In `_select_phase`, current coverage and rank waits are computed before declaration follow.
Keep those stage values for that call even if follow updates findings later.
Queued-ticket drops then release findings before decision selection.
The later held-cell exclusion reads the resulting finding state with that call's earlier coverage cells.
Preserve this order instead of globally recomputing everything after every mutation.

COUNT uses projected follow locations and suppresses its stdout.
It does not publish follow changes.
BORROW consumes the appropriate current facts and applies the existing writes.
Do not reuse COUNT results across changed dependencies without recomputing the affected partition.

Keep the two existing lock intervals and the sync-wait registration between them.
The second pass reads fresh availability, file hashes, and intervening row versions.
An unchanged second pass can reuse proof facts while repeating the messages the current code repeats.

The source index does not authorize ticket writes.
Existing assignment, approval, source, and pipeline checks retain their current behavior.
This design adds no guard, refusal, timeout, ticket limit, or source-size limit.

## Time and memory

Use these allocations as measurement goals, not enforced limits.

| Work | Ordinary refill target |
| --- | --- |
| Source hashing and live availability | 1.0 s, including an estimated 120–400 ms for four hash stages |
| New tree/blob facts and affected projections | 2.0 s |
| Selection, waits, and existing writes | 1.5 s |
| Interpreter and process overhead | 0.5 s |

Store large relations on disk and read them through ordered cursors.
Process one changed blob or comparison group at a time.
Release source and token objects after their dependent facts are written.
Keep returned candidate rows and selected ticket data in memory.
Do not retain every historical `RepositoryTip`, source string, or `SourceSites` object for the invocation.
Use file-backed SQLite staging when a grouping requires sorting.

The intended memory shape is interpreter overhead, SQLite working pages, the current source group, and selected output.
It does not include the full ledger or every historical tree in Python objects.
Measurements include Git subprocess memory and the total resident memory during refill.

An unusually large changed blob or required output can exceed the target.
This design processes that input normally and reports the measurement.
It does not add a size refusal or silently omit findings.

## Differential proof on the Mac

The old implementation is the behavioral oracle.
Use the pinned source revision above, or record the exact replacement revision before implementation begins.
Keep the old implementation available in the comparison worktree through final acceptance.

Phase 1 needs one saved ledger copy and one matching input set.
It does not need a general event-replay framework or the full matrix before the first improvement.
One comparison script restores that input set between the old and new runs.

1. Save the supplied Mac ledger copy and its matching files, configuration, and Git refs.
2. Run the old selector against a disposable restoration of those inputs.
3. Save its result, output, source changes, and timing.
4. Restore the same original inputs at the same disposable paths.
5. Run the new selector against that restoration.
6. Compare its result, output, source changes, and timing with the old run.

Phase 1 uses the measured cargo-liner input when its matching source state is available.
Existing focused tests cover selection cases absent from that input.
Later phases add differential fixtures only for the input groups they change.
The final replacement phase runs the complete accumulated matrix.

Use the real issue writer and renumbering code against the disposable vault.
Remap absolute paths in the copied ledger and configuration to that vault before either run.
Capture fixed board responses and mutable external observations needed by this input.
Both runs use those same responses at the same existing read points.
A recorded subprocess success alone cannot prove equal issue effects.
No comparison invocation uses the live natedev ledger.

Compare these results:

1. Exit code and typed result, including idle-family reasons.
2. Selected ticket id, kind, family, crate, ordered files, rules, cells, sites, findings, and decisions.
3. Stdout and stderr, including wait order, counts, duplicates, and suppressed COUNT output.
4. Plan bytes and pinned rule bytes.
5. Source ledger rows, claims, predecessors, sync waits, and follow provenance.
6. Issue bytes, completion reasons, ranking output, and unfinished publication state.

Exclude only new derived tables from source-state comparison.
Use a controlled clock and equivalent id counters rather than broadly deleting timestamps from the comparison.
Normalize only the known disposable root paths.
Record any existing hash-order variability separately before changing an ordering policy.

The full matrix grows with the phases:

| Phase | Added input cases |
| --- | --- |
| 1. Selection | One measured input; equal sort keys; first files; repeated selection; slice membership |
| 1. Mutable files | Changed hash with preserved timestamp/size; atomic replacement; new/deleted file or directory; source change between read stages |
| 2. Candidates | Zero sites; all generator kinds; trait-count changes; new trait name; manifest membership; ignores; shared pre-filter membership |
| 3. Judgments | Every tier and scope; explicit reads; removal/recreation; late judgments; corrections; rule generation and administrative changes |
| 3. Git and versions | Forward commits; merges; reverted edits; rewinds; branch replacement; modes; gitlinks; missing objects; rollback; deletes; primary-key changes |
| 4. Findings | Anchor changes; ambiguous moves; built rename chains; approval changes; open builds; footprint changes; old/new grouping keys |
| 5. Publication | Approval; unapproval; close/reopen; rename/delete; malformed decision body; interrupted refresh; ranking dependency changes |
| 5. Availability | Reservation changes and releases; `tree:.`; unavailable board; worktree renames; untracked files; new claims |
| 5. Complete selection | Own/count/borrow; equal family counts; structure; widened footprints; predecessor overlap; assigned resume; both refill passes |
| 5. Interleaving | Mutation before assignment; change during projection publication; change between passes; process interruption after source writes |
| 5. File sources | Rule-root replacement; complete file-source matrix with publication and both refill passes |

Compare intermediate cells, sites, footprints, and candidates when a final result differs.
This locates a missing dependency before the discrepancy reaches ticket selection.

Measure initial construction separately from unchanged and small-change runs.
Use fresh CLI processes for warm-index measurements.
Record wall time, CPU, RSS, subprocess RSS, bytes read/written, parsed blobs, SQL rows visited, and affected partitions.
Include file hashing, source construction, and subprocess work in the timing.

Repeat the cargo-liner case at tip `5142c3ca4` with its matching copied inputs when available.
The Mac proves equality and exposes algorithmic cost without natedev memory pressure.
Mac timing alone does not prove the natedev target.
Any later natedev measurement remains separate from this design task.

## Implementation phases

The measured Mac costs are selection 200 s, candidates 90 s, judgments 80 s, and sites 30 s.
These attributed costs total about 400 s.
The recorded complete runs took 395–489 s.
The estimates below use the 400 s attributed run, not a promise for every observed run.

Each phase includes its minimum storage, migration, and comparison support.
There is no separate foundation phase.
Each phase replaces its own production read path after its focused comparison passes.
It therefore ships a measured gain before the next phase starts.
The old comparison worktree supplies the reference behavior throughout.

| Phase | Shipping change and minimum foundation | Expected changed-stage time | Expected total after landing |
| --- | --- | --- | --- |
| 1. Repeated selection | Replace repeated finding hydration with one joined stage read; share eligible groups; batch blob reads for line counts; hash/cache parsed files | Selection: 200 s → 15–30 s | 215–230 s |
| 2. Candidate enumeration | Replace full exports with shared object facts and candidate partitions for all generators | Candidates: 90 s → 1–3 s | 126–143 s |
| 3. Judgment comparisons | Add changed-row versions, shared scope comparisons, and maintained cell states | Judgments: 80 s → 1–3 s | 47–66 s |
| 4. Sites and renames | Maintain finding proofs, rename endpoints, and footprints from changed dependencies | Sites: 30 s → 1–3 s | 18–39 s |
| 5. Remaining repeated selection and publication | Maintain groups and output inputs across invocations; complete the indexed default; remove obsolete reconstruction | Remaining selection and publication: about 1–2 s | 2–5 s for representative small changes |

These are engineering estimates; this document reports no new timings.
The first four ranges add the stated stage estimates without credit for improvements to unrelated stages.
The final range assumes no broad rule, scope, or ranking-output change.
Phase 5 removes remaining scans of unchanged groups and avoids repeated calls across all four measured categories.
Its 2–5 s estimate includes about 1–2 s of dirty-fact work shared by candidates, judgments, and sites.
It does not add the earlier 1–3 s category ranges unchanged.
Each phase records actual complete-command time and changed-stage time on the same Mac input before landing.
If measurements differ, the next phase uses the measured remaining costs to set its priority.

### Phase 1: first gain

Phase 1 changes `_refill_cells`, `_tickets_from`, `held_findings`, `read_slices`, and `fill_decision_batch` together.
It shares one stage's finding rows and eligible coverage groups across repeated reads.
It retains separate stage values after mutations.
It replaces per-file Git processes with batch reads and persistent line counts.
It also avoids repeated parsing of the small mutable file roots.

Only `refill_files` and the line-count rows of `refill_objects` are necessary initially.
The first `RefillContext` stores reuse within the invocation.
It reads mutable ledger rows again at existing mutation boundaries.
It does not persist mutable selection results before row-version invalidation exists.
This phase can reduce the measured selection cost even on its first run.
Its small comparison script uses the single restored input described above.

### Phase 2: remove the export

Phase 2 adds `refill_candidates` and candidate dependencies in `refill_dependencies`.
It extends `refill_objects` with tree, masked-source, and generator facts as required.
Git identities, file hashes, and explicit generator inputs provide candidate invalidation without the later ledger counters.
The phase compares each generator's incremental output with full enumeration after its relevant input changes.
Cross-file generators remain part of this phase, so no listed rule retains the ordinary full-export path.

### Phase 3: maintain judgment results

Phase 3 adds `refill_tables`, `refill_rows`, `refill_inputs`, and `refill_cells`.
It installs version triggers only for source tables used by these projections.
It extends dependency rows with scope membership, rule generations, history, and removal barriers.
It compares current cell results after judgment writes and Git changes.
This phase contains the counter foundation because its persistent cell results require that foundation.

### Phase 4: maintain finding results

Phase 4 adds `refill_findings` and the required finding/ticket version triggers.
It stores proof results, footprints, and built-rename endpoints together at each finding view.
It replaces `_refresh_site_rows` and `_built_renames` reconstruction on the refill path.
The phase adds finding moves, approval changes, and footprint cases to the comparison.

### Phase 5: complete the maintained selector

Phase 5 adds `refill_groups` and `refill_outputs`.
It replaces the remaining per-invocation group rebuilding with changed-group updates.
It adds completion, renumbering, availability, and publication dependencies.
It preserves own/count/borrow ordering and both lock intervals.
It runs the complete accumulated matrix before removing obsolete reconstruction code.

Phase 1 gains apply without a previously built index.
Later estimates describe a fresh CLI process after the relevant index has been built once.
Each phase measures its initial construction separately and includes that cost in its report.
An absent derived fact triggers only its own required calculation.
No phase requires all later tables or a partially implemented replacement engine.

Existing style suites include `test_style_plan_refill.py`, `test_style_plan_borrowing.py`, and `test_style_plan_checkpoint_refill.py`.
They also include generator, ledger, site, correction, decision, structure, and pipeline tests.
Use the repository's existing style validation entry points and pyright environment.
No tests or probes run as part of this document-only task.

## Removal

Remove reconstruction code after all production readers use the indexed implementation.
Keep shared semantic functions that still serve gates, reports, or tests.

| Code or storage | Removal or replacement |
| --- | --- |
| `_export_blobs` and refill's `style-listed-*` export path | Remove after candidate readers use immutable inputs |
| `_refresh_sites` catalog-wide marker comparison and full enumeration | Replace with dirty candidate partitions |
| Tip-wide `listed_rules` and duplicated `listed_sites` generations | Replace after all consumers use partition views; retain zero-result facts |
| `_coverage` as the refill's whole-repository computation | Replace with indexed cell queries; reports can stream those queries |
| `_refresh_site_rows` global reload and global cache clears | Replace with indexed histories and row dependencies |
| `_built_renames` full-history rebuild | Replace with stored rename edges and relevant-chain queries |
| `held_findings` id loop through `_held` | Replace bulk reads with joins; retain point lookup where needed |
| Repeated `rules.load_rules` calls within one observation stage | Replace with source-catalog references |
| Git subprocesses for line counts in `read_slices` and `fill_decision_batch` | Replace with blob facts |
| Refill's repeated `files_naming` searches and `EditedFiles` source retention | Replace with reference indexes and stored footprint facts |
| `_refill_cells` full finding scan | Replace held-cell exclusion with indexed aggregates |
| `_tickets_from` reconstruction of every unchanged group | Replace with dirty group updates and ordered queries |
| `ranked_work` full coverage/finding rebuild | Replace with contribution queries |
| `complete` and `run_follow` global discovery scans | Replace with completion and publication indexes |
| Refill's all-tree use of `GitReadCache.warm` | Remove; keep compatibility only for remaining explicit scan callers |

Retain `balanced_slices`, selection policy, existing proof rules, and phase rendering.
Retain the old comparison implementation only in the comparison worktree after removal.
Do not leave two production refill engines or a permanent old-engine recomputation fallback.

## Python and Rust

Keep the selector, transactions, and index updater in Python.
SQLite handles indexed joins and ordered row access.
Git supplies object and history reads through streaming processes.
This changes the amount of work before changing the language.

The measurements show repeated reconstruction, object retention, and process creation.
A Rust translation that reconstructs everything retains the same growth with repository and ledger history.
It also creates a second implementation of complex proof behavior during a correctness-sensitive change.

Measure tokenization, masked-source scanning, declaration reading, and sequence comparison after incremental storage is complete.
If those steps still dominate small changes, move their pure blob transformations into one Rust helper.
Use blob identity and reader version as its interface.
Keep its results byte-comparable with the Python implementation.
Send multiple blobs through one process rather than starting a process for each file or finding.

The recommendation is Python for this refill design.
Rust is a later option for measured parser cost, not a prerequisite for seconds.

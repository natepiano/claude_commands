# Next items — remove-clean-from-clean-fix

Work this plan surfaced but deliberately did not take on. Nothing here is a defect
in what the plan shipped; each is a pre-existing weakness that a phase opened the
file on and chose not to widen into. Writing an item here commits nobody to
building it — that decision happens when one is scheduled into a phase.

## 1. `PhaseStats`'s footer counters encode a state as an absent value

`clean_fix_report_parse.py` `PhaseStats.footer_ok`, `footer_fail`, and
`footer_total` are each `int | None`, and each surviving phase parser reads
`footer_total is None` to mean "this phase emitted no footer, so it is still
running". A phase
that finished reporting zero failures and a phase that never reported at all are
distinguished only by an absent number, in the file whose misreadings turn a
healthy run into a crashed one on the report. The three counts are also set
together from a single regex match at three sites, so nothing prevents them
drifting apart.

**What would satisfy it:** one member holding either "no footer seen" or a
footer's three counts together, so the running/finished distinction is the type
and the counts cannot disagree. Rework the three `phase_running` derivations and
the three assignment sites with it.

**Why not in the plan:** Phase 4 cut this file's phase model from six to four,
and phase-boundary detection there is positional. Adding a type rework to that
commit would have cost the reviewability the phase most needed.

## 2. Three conf-helper types encode state through flags and empty strings

- `project_add.py` `Project.workspace_root: Path | None` is valid only when
  `kind == "workspace_member"`, checked that way at `:305` — a discriminant and a
  payload in two fields that must agree and are not made to.
- `project_rename.py` `Plan.pending_path: Path | None` means "this rename has
  pending state to migrate" and is read as a presence test at `:435` and `:471`.
- `retarget_clean_fix.py` `DetectResult` is a `TypedDict` carrying a `match`
  boolean, a free-form `kind` string, and fields that are empty strings when
  `match` is false — match state spelled three ways at once.

**What would satisfy it:** one tagged project-role member replacing
`kind`/`workspace_root`; a pending-migration variant replacing `pending_path`; a
tagged match type replacing `DetectResult`'s boolean-plus-`kind`-plus-empties.

**Why not in the plan:** the rename phase already rewrites identifiers across a
broad file set. A type refactor riding along turns a mechanical sweep into a
design change nobody can review as either one.

## 3. Six helper types are named for representation, not role

`SectionResult` (`project_add.py`), `Plan`, `PlannedMove`, `PlannedMarker`
(`project_rename.py`), `DetectResult`, `CommitResult`
(`retarget_clean_fix.py`) say what a value *is* rather than what it is *for* —
a project-allowlist change, a project-rename migration plan, a history-state
move, a worktree-redirect match, a configuration-commit outcome.

**Do this one in the editor, not through an agent.** A global rename is exact and
instant there and slow and error-prone anywhere else; the right hand-off is this
list, not a phase. Item 2 should land first, since it changes which types exist.

## 4. The report model hides domain outcomes behind generic types and strings

The report parser (`clean_fix_report_parse.py`, later `fix_report_parse.py`)
exposes `Cell`, which does not say that it is one project's outcome for one
report phase, and `ParseResult`, which says only how the value was obtained.
`Warning` is also stored in `ParseResult.running`, so its name is false for live
progress. `Cell.state` and `ParseResult.status` are free-form `str`, while
`Cell.reason = ""` makes payload validity depend on caller convention.

**What would satisfy it:** replace `Cell` with a tagged `ProjectPhaseOutcome`
whose variants state `NotRun`, `Succeeded`, `Failed`, `Skipped`, or `Running` and
carry only the payload appropriate to that state; rename `ParseResult` to
`PipelineStatusReport`; and split `Warning` into `ProjectFailure` and
`RunningProjectStatus`. Preserve `re.Match[str] | None` only at the regex
boundary and convert before data enters the report model.

**Why not in the plan:** Phases 8 through 10 are the brand/path rename and final
verification sweep. Phase 4's six-phase fixture and four-phase regression test
now provide the oracle for a dedicated report-model refactor without coupling it
to those renames.

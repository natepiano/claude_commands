# Fix pipeline — deferred items

Known, unscheduled weaknesses in `scripts/fix/` and
`scripts/make_a_worktree/retarget_fix.py`. None is a defect in shipped
behavior; each is a pre-existing representation problem in working code.
Listing an item here commits nobody to building it — that decision happens when
one is scheduled into a phase.

The pipeline's as-built is `docs/as-built/fix-pipeline.md`. Its **Ruled out**
section is authoritative over this file: anything named there is settled and
must not be re-proposed from here.

## 1. Four conf-helper types encode state through flags, optionals, and empty strings

- `project_add.py` `Project.workspace_root: Path | None` is valid only when
  `kind == "workspace_member"`, checked that way at `:305` — a discriminant and a
  payload in two fields that must agree and are not made to.
- `project_rename.py` `Plan` couples `pending_path: Path | None` to
  `pending_project_root_changed: bool`. `build_plan()` derives them separately at
  `:368-380`; `print_plan()` tests the boolean at `:434` and interpolates the
  optional at `:435`, while `update_pending_project_root()` independently reads
  `None` as "no pending JSON" before the unconditional call at `:471` — so a
  state where the two disagree is representable.
- `DetectResult` in `scripts/make_a_worktree/retarget_fix.py` is a
  `TypedDict` carrying a `match` boolean, a free-form `kind` string, and fields
  that are empty strings when `match` is false — match state spelled three ways
  at once.
- `CommitResult` in that same helper couples `committed: bool` with mutually
  exclusive `commit` and `reason` strings. Success needs a commit and an empty
  reason; every no-op and every failure needs an empty commit and free-form
  reason text.

**What would satisfy it:** one tagged project-role member replacing
`kind`/`workspace_root`; a tagged pending-migration state replacing both
`pending_path` and `pending_project_root_changed`, whose variants distinguish no
pending JSON, pending JSON already current, and pending JSON needing a root
update; a
tagged worktree-redirect match replacing `DetectResult`; and a
`ConfigurationCommitOutcome` whose variants state `Committed`, `Unchanged`,
`RepositoryUnavailable`, `StageFailed`, and `CommitFailed`.

## 2. Six helper types are named for representation, not role

`SectionResult` (`scripts/fix/project_add.py`), `Plan`, `PlannedMove`,
`PlannedMarker` (`scripts/fix/project_rename.py`), `DetectResult`, and
`CommitResult` (`scripts/make_a_worktree/retarget_fix.py`) say what a value *is*
rather
than what it is *for* — a project-allowlist change, a project-rename migration
plan, a history-state move, a worktree-redirect match, or a configuration-commit
outcome.

**Do this one in the editor, not through an agent.** A global rename is exact and
instant there and slow and error-prone anywhere else; the right hand-off is this
list, not a phase. Item 1 should land first, since it changes which types exist.

## 3. The report model hides domain outcomes behind generic types and strings

The report parser (`scripts/fix/fix_report_parse.py`)
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

**Oracle:** `scripts/fix/tests/fixtures/six-phase-run.log` and the four-phase
regression test in `scripts/fix/tests/test_report_parse_phases.py` cover this
file's phase model, so a report-model refactor can be validated differentially.
`PhaseStats`'s `footer_ok`/`footer_fail`/`footer_total` are **not** in scope
here — the as-built rules that reshaping settled.

## 4. The flow renderer hides geometry state behind `Bbox | None`

`scripts/fix/render-flow.py` names its
coordinate tuple `Bbox`, which states a representation rather than what the four
numbers bound, and returns `Bbox | None` from `bbox_from_points`, `union_bboxes`,
`get_element_bbox`, `get_node_bbox`, and `get_content_bbox`. Depending on the
caller, the absent value means no points, non-drawable or unsupported SVG
content, a node with no supported shape, or no graph bounds at all.
`inject_clusters()` then drops a declared cluster whose members produce no
bounds, silently, in the same run that reports having parsed that cluster.

**What would satisfy it:** a semantic `SvgBounds` type replacing `Bbox`;
ElementTree attribute optionals converted at the XML boundary; tagged geometry
outcomes that tell non-drawable content apart from an unsupported shape; and a
declared node or cluster with no measurable bounds failing with a diagnostic
naming it rather than disappearing. Cover polygon and ellipse bounds, an
unsupported element, and a cluster whose members cannot be measured.

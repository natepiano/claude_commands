# Next items — remove-clean-from-clean-fix

Work this plan surfaced but deliberately did not take on. Nothing here is a defect
in what the plan shipped; each is a pre-existing weakness that a phase opened the
file on and chose not to widen into. Writing an item here commits nobody to
building it — that decision happens when one is scheduled into a phase.

## 1. `PhaseStats`'s footer counters encode a state as an absent value

The report parser (`scripts/fix/fix_report_parse.py`)
defines `PhaseStats.footer_ok`, `footer_fail`, and `footer_total` as
`int | None`, and each surviving phase parser reads
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

## 2. Four conf-helper types encode state through flags, optionals, and empty strings

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

**Why not in the plan:** the rename phase already rewrites identifiers across a
broad file set. A type refactor riding along turns a mechanical sweep into a
design change nobody can review as either one.

## 3. Six helper types are named for representation, not role

`SectionResult` (`scripts/fix/project_add.py`), `Plan`, `PlannedMove`,
`PlannedMarker` (`scripts/fix/project_rename.py`), `DetectResult`, and
`CommitResult` (`scripts/make_a_worktree/retarget_fix.py`) say what a value *is*
rather
than what it is *for* — a project-allowlist change, a project-rename migration
plan, a history-state move, a worktree-redirect match, or a configuration-commit
outcome.

**Do this one in the editor, not through an agent.** A global rename is exact and
instant there and slow and error-prone anywhere else; the right hand-off is this
list, not a phase. Item 2 should land first, since it changes which types exist.

## 4. The report model hides domain outcomes behind generic types and strings

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

**Why not in the plan:** Phases 8 through 10 are the brand/path rename and final
verification sweep. Phase 4's six-phase fixture and four-phase regression test
now provide the oracle for a dedicated report-model refactor without coupling it
to those renames.

## 5. The flow renderer hides geometry state behind `Bbox | None`

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

**Why not in the plan:** Phase 9 re-renders the unchanged diagram after the
file/basename rename and the `PHASE_CLUSTER_IDS` repair, and must prove that SVG
is byte-identical to the pre-edit output. Phase 10 re-renders after deliberate
label and comment rebranding, so byte identity is not expected there.
Restructuring the geometry model would confound both the rename check and the
prose-only one, so it belongs after this plan.

**Revealed by:** Phase 6.

## 6. `codex exec` rejects `--full-auto`, so every evaluation worker fails

`scripts/fix/style-eval-all.sh:417` passes `--full-auto` to `codex exec`. The
installed `codex` no longer accepts it: `error: unexpected argument '--full-auto'
found`, with the tip `to pass '--full-auto' as a value, use '-- --full-auto'`.
Every worker exits 2 before invoking its history helper, so the eval stage
reports 27 failures out of 30 and the fix stage skips every one of them with
`eval-failed`. Retry does not help, because the flag is wrong on both attempts.
The pending work is kept for resume and a per-failure report is written under
`nate_style/.history/.failures/`, so nothing is lost — but nothing is evaluated
either.

**What would satisfy it:** the eval launcher passes flags the installed `codex
exec` accepts, and a forced `run_once` reports a non-zero eval `ok` count.
`scripts/fix/style-eval-review-all.sh` and `scripts/fix/style-fix-worktrees.sh`
build their own `codex` argument lists and should be checked against the same
binary at the same time.

**Why not in the plan:** the flag is byte-identical at the pre-plan commit and
has nothing to do with removing the clean stage. It surfaced only because this
plan's final phase forced a full `run_once`, which is the first run in this
window to reach the eval stage at all.

**Revealed by:** Phase 10.

## 7. A configured project points at a path that no longer exists

`scripts/fix/fix.conf` lists `hana/crates/bevy_kana`, and every run skips it with
`SKIP: bevy_kana (target path not found: /Users/natemccoy/rust/hana/crates/bevy_kana)`.
The skip is emitted once per stage that enumerates projects, so a single missing
directory produces a recurring line in every report for a project that cannot be
worked on.

**What would satisfy it:** the entry names a path that exists, or it is removed
from the allowlist, and a forced `run_once` emits no `target path not found`
line.

**Why not in the plan:** whether the crate moved or was deleted is a fact about
the `hana` workspace, not about this repository, and the entry is unchanged at
the pre-plan commit.

**Revealed by:** Phase 10.

## 8. An unquoted heredoc evaluates a backtick in the fix agent's prompt

`scripts/fix/style-fix-worktrees.sh:679` opens the agent prompt with
`cat > "$prompt_file" <<PROMPT_EOF` — an unquoted delimiter, so the shell expands
the body. Line 835 writes ``N remaining: `path:line`, ...`` with live backticks,
so the shell runs `path:line` as a command and prints
`style-fix-worktrees.sh: line 679: path:line: command not found` once per
invocation. The message names the heredoc's opening line, not the offending one,
which sends anyone reading the log to the wrong place. The prompt still reaches
the agent with an empty span where the example should be, so the instruction the
line was written to give is silently missing.

The file documents this exact hazard at `:673-676` and warns against command
substitution around the heredoc; the guard is right and one line slipped past it.

**What would satisfy it:** the prompt body no longer runs anything at render
time — either the delimiter is quoted (`<<'PROMPT_EOF'`, with every intended
expansion re-introduced deliberately) or the backticks at `:835` are escaped —
and a forced `run_once` reaches the fix stage with no `command not found` line.

**Why not in the plan:** the line is byte-identical at the pre-plan commit, and
the surrounding heredoc is untouched by the rename.

**Revealed by:** Phase 10.

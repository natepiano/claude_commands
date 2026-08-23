---
description: Run the Rust lint pipeline, or run its style review alone without repeating the rest of the pipeline
---

<InvocationModes>
Parse these case-insensitive whole-word control tokens before any remaining
arguments can reach `lint clippy`, then strip them from `$ARGUMENTS`:

- `style-only` sets `STYLE_ONLY = true`. This invocation runs
  <LoadOperationSwitches/> and then exactly one <StyleReview/>. It does not run
  the cache, mend, Clippy, rustdoc, batch-fix, or formatting stages.
- `no-style` sets `NO_STYLE = true`. This invocation runs the normal pipeline
  with `LINT_OP_STYLE_REVIEW=off` for this invocation only; it does not edit
  `config/lint.conf`.

If both tokens are present, STOP and report that `style-only` and `no-style`
are mutually exclusive. Both variables default to false.
</InvocationModes>

<AutoProceed>
If $ARGUMENTS contains the token `auto-proceed` (injected by /plan:delegate and
the codex work orders it composes), this run is non-interactive: strip the token
before any remaining arguments reach `lint clippy`, and <BatchDecisionPoint/>
reports the batch then immediately executes it as **proceed** — no stop, no user
wait. Auto-proceed does NOT soften the hard stops in <RunMendFix/> or the
environmental-failure handling anywhere in this skill — those still stop.
</AutoProceed>

<LoadOperationSwitches>
Run:
```bash
bash ~/.claude/scripts/lint/lint_config.sh export
```

It prints one `LINT_OP_<NAME>=on|off` line per check, read from
`~/.claude/config/lint.conf` (edited with `/lint_config`). A check whose value
is `off` is **skipped entirely** — do not run its command, do not substitute an
equivalent by hand, and do not create todos from it.

After reading the file, if `NO_STYLE = true`, set
`LINT_OP_STYLE_REVIEW=off` for this invocation. If `STYLE_ONLY = true`, ignore
the other operation switches: execute <StyleReview/> when
`LINT_OP_STYLE_REVIEW=on`, otherwise report `Style review: Off` and stop. The
global switch remains authoritative for whether a requested style-only pass is
allowed to run.

| Variable | Gates in this skill |
|---|---|
| `LINT_OP_CACHE` | STEP 1 <CheckCachedResults/> |
| `LINT_OP_MEND` | STEP 2 <RunMend/> and STEP 3 <RunMendFix/> |
| `LINT_OP_STYLE_REVIEW` | STEP 4 <StyleReview/> |
| `LINT_OP_CLIPPY` | STEP 5 <RunClippy/> |
| `LINT_OP_DOC` | STEP 5b <RunDoc/> |
| `LINT_OP_FMT` | STEP 8 <RunFmt/> |

These are the same switches `scripts/delegate/verify.sh` and clean-fix read —
one key per check, no per-consumer override — so `clippy=off` here means clippy
is off in every delegate phase too.

Two interactions the switches change:

- **`LINT_OP_MEND=off` with `LINT_OP_CACHE=on`.** The cache script has no
  knowledge of the switches, so it still prints mend counts and can still emit
  `resume: STEP 3`. Treat that directive as `STEP 4`, ignore the cached
  `=== lint mend (manual) ===` block, and put none of it in the batch.
- **A disabled stage in <ReportFindings/>.** Keep its row and write `Off` in
  both Result and Action. Never report a skipped stage as clean — an unrun
  check found nothing because it did not run.

In the normal pipeline, if every check is off, print the Findings table with
all rows `Off` and stop.
If the script fails, report the error and stop; do not assume defaults.
</LoadOperationSwitches>

<LoadStyleGuide>
Load the Rust style guide by running:
```bash
zsh ~/.claude/scripts/rust_style/load-rust-style.sh
```
Confirm using the script summary line. Then proceed.
</LoadStyleGuide>

<CheckCachedResults>
Run the cache check script:
```bash
bash ~/.claude/scripts/lint/check_cache.sh .
```

**Exit 1** — cache miss. Proceed to STEP 2 (<RunMend/>).

**Exit 0** — cache hit. Print the status table, then obey the `resume:` line the
script emits. It is computed from the cached run; do not re-derive it.

| `resume:` | What to do |
|---|---|
| `NONE` | Print the status table and exit — complete no-op. |
| `STEP 3` | The cached run has auto-fixable mend findings that were never applied — cargo-port runs mend in check mode, so a cache hit is always a pre-fix snapshot. Enter at STEP 3, continue 3 → 4 → 5 → 5b → 6 → 7 → 8 → 9. Applying fixes rewrites source, so clippy and doc must genuinely re-run. |
| `STEP 4` | Nothing to auto-fix. Enter at STEP 4, then go to STEP 6 — at STEP 5/5b reuse the `=== lint clippy ===` / `=== lint doc ===` output already printed rather than re-running. If STEP 4 edited files, run 5/5b for real instead. Continue 6 → 7 → 8 → 9. |

Reaching STEP 7 by any route still means stopping at `<BatchDecisionPoint/>` and
waiting for user approval before any edits (in auto-proceed mode the gate
reports and proceeds instead — see <AutoProceed/>). A resume directive never
skips the decision gate.

Only the **manual** mend findings are printed, under `=== lint mend (manual) ===`
— the ones `mend --fix` cannot apply. Those are the ones that belong in the
batch todo list. Auto-fixable findings are counted, never listed, because STEP 3
handles them; do not turn them into todos. If the manual listing is truncated,
the script prints the path to the full listing — read that file rather than
working from the excerpt.

The script reads lint-runs' `latest.json`, waits if a run is still in progress,
takes per-command pass/fail from its `commands[]` array, and rejects the cache
when any `*.rs`/`*.toml` is newer than the run.
</CheckCachedResults>

<RunMend>
Execute: `~/.claude/scripts/lint/lint mend`

The `lint` wrapper owns the cargo-mend exception: it runs mend with
`RUSTC_WRAPPER=` because wrapper tools can otherwise receive `cargo-mend` in
the rustc position and fail before code is checked. Do not run `cargo mend`
directly from this skill.

Error Handling:
- **Environmental Issues (Stop execution):** If mend fails due to missing Cargo.toml or missing toolchain, inform user: "cargo mend cannot run - environment setup required." Then exit.

Analyze output:
- If mend reports **fixable items**, proceed to <RunMendFix/>.
- If mend reports **zero issues**, skip to <RunClippy/>.
- If mend reports **only unfixable items**, note them and skip to <RunClippy/>.
</RunMend>

<RunMendFix>
Execute: `~/.claude/scripts/lint/lint mend --fix`

The `lint` wrapper applies the same cargo-mend wrapper exception noted in
<RunMend/>.

Error Handling:
- **Fix reverted due to compiler error (HARD STOP — capture reproduction):** If
  mend applies a fix, the resulting code fails to compile, and mend reverts it,
  this is a bug in cargo mend — it should never fail on a fix it claims it can
  apply. **STOP immediately. Do NOT proceed, do NOT retry, do NOT attempt the
  fix manually.** The working tree is now back at its pre-fix state, which is
  exactly the state needed to reproduce the bug. Present the following so the
  user can copy it verbatim and reproduce / fix cargo mend:
    1. The full `~/.claude/scripts/lint/lint mend --fix` output, including
       the lint(s) mend tried to fix and the compiler error(s) that triggered
       the revert.
    2. The exact command that reproduces it: `~/.claude/scripts/lint/lint mend --fix`
    3. `git rev-parse HEAD` and `git status --short` output, confirming the
       working tree matches the reproduction state.
  Then end your turn and wait for the user. Do NOT proceed to clippy.
- **Any other failure** (non-zero exit, error output, unexpected behavior):
  **STOP immediately**. Report the error output to the user and ask what to do.
  Do NOT proceed to clippy.

If successful: Note any remaining unfixable items from the earlier `cargo mend` run, then continue with the next step in <ExecutionSteps/>.
</RunMendFix>

<RunFmt>
Execute: `~/.claude/scripts/lint/lint fmt`

This step runs whether mend fixed anything, found nothing, or had only unfixable items. `LINT_OP_FMT=off` is the only thing that skips it.

Check `git diff` after running to determine if fmt applied any changes.
- If diff is non-empty: fmt applied formatting fixes. Note this for the completion summary.
- If diff is empty: no formatting changes needed. Note this for the completion summary.
</RunFmt>

<RunClippy>
The `lint` wrapper supplies `--all-targets --all-features -- -D warnings` and
resolves package scope per run: only the workspace members the working tree
changed, widening to `--workspace` when a root manifest, lockfile, or shared
tool config changed, when nothing changed, or when over half the members did.
Pass `--workspace` or `-p <pkg>` in `$ARGUMENTS` to choose the scope yourself.

Execute: `~/.claude/scripts/lint/lint clippy ${ARGUMENTS:-}`

If $ARGUMENTS provided, use as additional flags — after removing the
`auto-proceed`, `style-only`, and `no-style` tokens, which are mode switches,
not clippy flags (see <InvocationModes/> and <AutoProceed/>).
If different base configuration needed, user can override CLIPPY_FLAGS.

Error Handling:
- **Environmental Issues (Stop execution):** If clippy fails due to missing Cargo.toml, network issues, or missing toolchain, inform user: "Clippy cannot run - environment setup required. Check for Cargo.toml and valid Rust workspace." Then exit.
- **Compilation Errors (Process as todos):** If clippy fails due to compilation errors, treat these as high-priority todos alongside any warnings found.

Capture all output for analysis - both successful warnings and compilation errors become todos.
</RunClippy>

<RunDoc>
Execute: `~/.claude/scripts/lint/lint doc`

This mirrors the cargo-port `doc` lint command. `-D warnings` promotes every
rustdoc lint (broken intra-doc links, invalid codeblock attributes, bare URLs,
unescaped backticks, …) to an error, so a non-zero exit means a doc problem.
`--no-deps` keeps dependencies' docs (and their warnings) out of the run.

Error Handling:
- **Environmental Issues (Stop execution):** If the command fails due to a missing Cargo.toml, network issues, or missing toolchain, inform user: "cargo doc cannot run - environment setup required." Then exit.
- **Doc Errors (Process as todos):** rustdoc errors become todos alongside clippy findings.

Note: `missing_docs` is a rustc compile-time lint, not a rustdoc one — it does NOT surface here. It is caught by clippy/check when configured in `[lints.rust]`.

Capture all output for analysis - rustdoc errors become todos.
</RunDoc>

<ReportFindings>
Present a summary before proceeding to todos, as a table — one row per lint
stage, no prose around it:

## Findings
| Stage | Result | Action |
|---|---|---|
| Mend | [one of: "Fixed N issues" \| "No issues found" \| "No fixable issues found" \| "Off"] [if unfixable: ", N unfixable"] | [Applied \| N todos below \| Off \| —] |
| Style review | [one of: "N violations fixed" \| "All changes conform" \| "No uncommitted changes" \| "Off"] | [Applied \| Off \| —] |
| Clippy | [one of: "N issues across M files" \| "No issues found" \| "Off"] | [N todos below \| Off \| —] |
| Doc | [one of: "N rustdoc errors across M files" \| "No issues found" \| "Off"] | [N todos below \| Off \| —] |

The **Action** column carries what used to be explanatory prose: mend and style
fixes are already applied (`Applied`), unfixable mend issues / clippy issues /
rustdoc errors become todos (`N todos below`), and a clean stage is `—`. Do not
restate any of that in sentences under the table.

A stage disabled by <LoadOperationSwitches/> reads `Off` in **both** columns.
Keep the row — dropping it, or writing "No issues found" for a stage that never
ran, hides the fact that the check was skipped.
</ReportFindings>

<CreateBatchTodoList>
Create a comprehensive todo list combining all clippy, rustdoc, AND unfixable mend issues:
- Group related issues in same function/struct into single todos when logical
- Each todo includes fix description and affected file locations
- Label each todo with its source (clippy, doc, or mend) for clarity
- For each clippy todo, run the `lint:` frontmatter lookup described in
  `<FixingGuidelines/>` and include the matched rule file in the todo (e.g.
  `rule: never-allowclippytoomanylines.md`). Only include this field when a
  matching rule file exists — omit it otherwise rather than writing "none".
- Present complete batch for user decision
- Note: Hook automatically provides cargo check feedback on edit - no explicit build commands needed
</CreateBatchTodoList>

<BatchDecisionPoint>
**Auto-proceed mode (see <AutoProceed/>):** print the same Issues Found block
and per-todo details below, then immediately execute <BatchExecution/> as
**proceed**. Skip the Available Actions menu; do not end the turn.

**Otherwise this is a hard gate. STOP here. Do NOT edit any files until the user
has selected one of the Available Actions below. This applies even if there is
only one issue — the user must approve every fix before execution.**

Present the complete batch of fixes exactly as follows — **one table, one row
per todo**. No paragraph-per-issue write-ups; the columns carry the metadata.

## Issues Found
[total] issues — mend (unfixable) [mend_count] in [mend_file_count] files, clippy [clippy_count] in [clippy_file_count] files, doc [doc_count] in [doc_file_count] files

| # | Src | File:line | Lint | Rule | Fix |
|---|---|---|---|---|---|
| 1 | mend | src/project/cargo/parse.rs:78 | forbidden_pub_crate | use-narrowest-visibility | Re-export `ManifestTargets` through `cargo/mod.rs` and `project/mod.rs`, matching `GitRepoPresence`. |

Column rules:
- **#** — todo number, also the key for any Notes entry below.
- **Src** — `mend` / `clippy` / `doc`, lowercase.
- **File:line** — repo-relative path and line, nothing else.
- **Lint** — the lint or rule name that fired. For a style-review violation with
  no lint name, use the rule's short name.
- **Rule** — matched style-guide filename from the `lint:` frontmatter lookup,
  `.md` dropped for width. `—` when no rule file matches (do not write "none",
  do not leave the cell blank).
- **Fix** — the fix approach in **one sentence**, imperative, naming the concrete
  edit. This is the last column so its wrapping does not shift the others.

Anything that does not fit one sentence — a rationale, a constraint that rules
out the obvious fix, a pre-existing off-rule pattern deliberately not copied —
goes in a `### Notes` section under the table, prefixed with the row number it
belongs to (`**2.** …`). Do not inflate the Fix cell to carry it, and do not add
notes for rows that do not need one; omit the section entirely when every row is
self-explanatory.

**Notes must be readable at a glance.** Give each note a short bolded lead-in
naming what that paragraph answers, and put one idea in each paragraph. The
questions a note usually needs to answer, in this order:

1. **What is being flagged** — the specific thing in the code, with the
   surrounding pattern that makes it stand out.
2. **Why the fix is safe** (or what makes it risky) — what keeps working, and
   which call sites move.
3. **How it was verified** — which tool answered, and what the fallback covers
   if the preferred one did not.
4. **Where the rule comes from** — only when the `lint:` lookup found nothing
   and the rule had to be traced somewhere else.

Skip any of these that do not apply. A note that only needs one sentence stays
one sentence.

#### Do not write notes like this

The following is a real note that was rejected as unreadable. Every fact in it
is correct; the problem is that four unrelated findings are fused into one
unbroken block, so nothing can be located without reading all of it:

> **1.** No style-guide file carries `review-pub-mod` in its `lint:`
> frontmatter, so the mechanical lookup found nothing. `use-narrowest-visibility.md`
> still governs it in prose: "`pub mod` is forbidden by never-use-pub-mod" (that
> referenced rule file isn't present in `~/rust/nate_style/rust/`). The fix is
> zero-call-site: all five public items are already re-exported from the crate
> root at `lib.rs:80-84` (`ImageDelay`, `Outputs`, `Settings`, `State`,
> `register`), and the sole external consumer — `crates/hana/src/video_plane/mod.rs:169`
> — calls `hana_mimesis_tools::register_image_delay`, the re-export, not the
> module path. `image_delay` is also the only `pub mod` in that file; the other
> 17 are private `mod` with `pub use` re-exports. LSP `findReferences` on the
> module declaration returned nothing (rust-analyzer doesn't resolve module decls
> as symbols), so coverage here rests on ripgrep across every `.rs` in the
> workspace, which is exhaustive for a module path since it can't be reached
> through a type alias or generic dispatch.

What is wrong with it: it opens on rule provenance (the least useful fact)
instead of on what the code does; the verification caveat is buried in the last
clause; parenthetical asides carry load-bearing information; and the reader has
to hold four threads at once because none of them are separated or labeled.

#### Write it like this instead

> **1. What mend is flagging.** `hana_mimesis_tools/src/lib.rs` declares 18
> modules. Seventeen are private (`mod foo;`). One — `image_delay` — is
> `pub mod`. The style guide bans `pub mod`.
>
> **Why making it private is safe.** Nothing gets hidden, because the crate root
> already re-exports the five things callers need (lines 80–84): `ImageDelay`,
> `Outputs`, `Settings`, `State`, `register`. Those re-exports keep working when
> the module itself goes private. The only code outside the crate that touches
> `image_delay` is `crates/hana/src/video_plane/mod.rs:169`, and it calls
> `hana_mimesis_tools::register_image_delay` — the re-export, not the module
> path. So no caller changes.
>
> **How I checked.** LSP was the right tool but returned nothing: rust-analyzer
> doesn't treat a `mod` declaration as a findable symbol. I fell back to ripgrep
> for `image_delay` across every `.rs` file in the workspace. For a module name
> that's a complete check — unlike a type, a module path can't be reached through
> an alias or generic dispatch, so if grep doesn't see it, it isn't there.
>
> **Where the rule lives.** The skill looks up a lint name in style-guide `lint:`
> frontmatter. Nothing matches `review-pub-mod`. The ban is stated in passing
> inside `use-narrowest-visibility.md`, which cites a `never-use-pub-mod` file
> that doesn't exist in the guide directory. The rule is real; its home file is
> missing.

Same facts, same length. The difference is that a reader who only wants to know
whether the fix is safe can find that answer without reading the other three
paragraphs.

## Available Actions
- **proceed** - Fix all issues using standard clippy guidance
- **change** - Modify the approach (specify changes)
- **stop** - Cancel fixes without making changes

Please select one of the keywords above.

**After printing this block, end your turn. Wait for the user's next message.
Do not call any Edit/Write/Bash tools until then.**
</BatchDecisionPoint>

<BatchExecution>
**proceed**: Apply all fixes systematically following <FixingGuidelines/>.

**Hook Integration**: Expect automatic cargo check context injection after each edit:
- Hook will automatically run cargo check and inject error/warning context
- Take immediate action on injected error/warning information to resolve issues
- If cargo check context is insufficient for diagnosis, run `cargo build` to get full error details
- Continue fixing systematically, responding to each hook feedback cycle

**change**: Ask user: "What modifications would you like to the fixing approach?" Then apply their specified changes to the batch.

**stop**: Exit without applying any fixes.

After batch completion: Display summary of fixes applied and any remaining issues.
</BatchExecution>

<FixingGuidelines>
**Important rules:**
- Do not fix warnings by marking code as dead - remove dead code
- Do not fix warnings by prefixing arguments/variables with _ - remove if unused

**Use LSP before any fix that changes a name or signature.** When the fix is a
rename, removal, visibility narrowing, or type change, run LSP `findReferences`
on the target first to enumerate every call site. ripgrep misses references
through type aliases, re-exports, and generic dispatch — LSP doesn't. Apply
the fix at every reference returned. For pure intra-function fixes (rewriting
a closure, inlining a return, removing a needless `&`) LSP is unnecessary.

LSP availability: `LSP` tool is loaded when `ENABLE_LSP_TOOL=1` is in env (in
your settings.json). If unreachable, fall back to ripgrep but expand the scope
to the whole crate (not just the cited file) and note the limitation in the
fix description.

**Consult the style guide per-lint before fixing.** For every clippy finding,
before proposing a fix, grep the loaded style guide for the lint name in the
`lint:` frontmatter property:

```bash
grep -l "^lint:.*\b<lint_name>\b" ~/rust/nate_style/rust/*.md docs/style/*.md 2>/dev/null
```

If a file matches, read it and apply the rule it prescribes (often the
"extract helpers / orchestrator pattern", "no bare allow", or
"test-module allow boilerplate"). Cite the rule file in your fix
description.

If no file matches, state that explicitly ("no style-guide rule governs
`<lint_name>`") and proceed with a judgment call.

Rationale: the style guide has been known to be skipped at fix time even
when loaded. The `lint:` frontmatter property is the single source of
truth that maps clippy lints to the rule that governs them.
</FixingGuidelines>

<StyleReview>
**This step runs even if mend and clippy found zero issues.** `LINT_OP_STYLE_REVIEW=off` is the only thing that skips it.

**Do this step yourself — do not delegate it to a subagent** (Task/Agent tool, Codex sub-session, etc.). Run the diff commands and the rule-by-rule walk inline in this conversation so the user can watch progress rule-by-rule instead of waiting on an unauditable subagent turn.

1. Build the combined diff of uncommitted work, then the additions-only text from it. **Untracked files are always included** — a new file is entirely added code, so `git diff --no-index /dev/null <file>` renders it as all-additions. Never review only tracked changes:
   ```bash
   {
     git diff
     git ls-files --others --exclude-standard -z \
       | xargs -0 -I{} git diff --no-index /dev/null "{}" 2>/dev/null
   } > /tmp/claude/style-review.diff
   grep '^+' /tmp/claude/style-review.diff | grep -v '^+++' > /tmp/claude/style-review-additions.txt
   ```
   The `git diff --no-index` exit status 1 (differences found) is expected, not an error. The `.diff` file (with `+++`/`@@` headers) drives the banned-words scan so findings report real source `path:line`; the `-additions.txt` (added lines only) is your reading aid for the rule walk. If `-additions.txt` is empty, report: "No uncommitted changes to review." and skip remaining steps.

2. If the `=== STYLE_CHECKLIST ===` section is not already in context, execute <LoadStyleGuide/> now. The checklist lists every rule by number and name.

3. **Systematic walk**: For each rule in the checklist, check the additions-only diff. Present results in a table:

   ```
   | # | Rule | Result |
   |---|---|---|
   | 1 | Rule name | Pass / **VIOLATION: description** / Skip (reason) |
   ```

   - **Pass**: additions conform to this rule
   - **VIOLATION**: describe what violates and where
   - **Skip**: rule does not apply to anything in the diff (e.g., "no module declarations changed", "no format strings", "no `#[allow]` added"). Use a short reason.

   **Banned-words rule (special handling):** When the checklist contains a "no
   banned words" / "forbidden words" rule, do **not** enumerate the stems
   yourself, do **not** write a patterns file, and do **not** build an inline
   regex — the bare stems will trip the PostToolUse hook. Instead, pipe the
   combined diff through the canonical scanner's `--diff` mode:

   ```bash
   python3 ~/.claude/scripts/hooks/banned_words_lib.py --diff < /tmp/claude/style-review.diff
   ```

   Exit 0 = Pass. Exit 1 = VIOLATION; each output line is `path:lineno: stem:
   <line>`, where `path:lineno` is the **real source location** of the added
   line (untracked files included). `--diff` scans added lines only, so
   pre-existing lines in tracked files are not re-flagged. The script path
   contains `banned_words_lib`, which the hook's introspection bypass
   recognizes, so neither the command nor its output re-trips the scanner or
   bumps counters.

4. Fix all violations found.
5. After fixes, report: "Style review complete — N violations fixed." or "Style review passed — all changes conform to the style guide."
</StyleReview>

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER.** Every numbered step below is gated by
<LoadOperationSwitches/> — a step whose switch is `off` is skipped outright, and
the pipeline continues at the next enabled step.

**STEP -1:** Execute <InvocationModes/> and <AutoProceed/> — parse and strip all control tokens before any command receives `$ARGUMENTS`.
**STEP 0:** Execute <LoadOperationSwitches/> — read which stages are enabled. This step is never skipped.
**STYLE-ONLY EXIT:** If `STYLE_ONLY = true`, execute <StyleReview/> when enabled, report its result, and stop. Run none of STEP 1 through STEP 9.
**STEP 1:** Execute <CheckCachedResults/> — check for fresh lint-runs results. On a cache hit, re-enter the pipeline at the step named by the script's `resume:` line and continue through the remaining steps in order. A cache hit never skips STEP 3 when there are auto-fixable mend findings, and never skips the STEP 7 decision gate.
**STEP 2:** Execute <RunMend/> — run `~/.claude/scripts/lint/lint mend` to check for issues
**STEP 3:** If fixable items found, execute <RunMendFix/> — run `~/.claude/scripts/lint/lint mend --fix`. If it fails, STOP and ask user.
**STEP 4:** Execute <StyleReview/> — evaluate diff against style guide rules (loads style guide only if diff is non-empty). Runs regardless of what earlier steps found; only `LINT_OP_STYLE_REVIEW=off` skips it.
**STEP 5:** Execute <RunClippy/>
**STEP 5b:** Execute <RunDoc/> — run rustdoc as a lint with `~/.claude/scripts/lint/lint doc`
**STEP 6:** Execute <ReportFindings/> — present mend, clippy, and doc summary (fmt runs later, at STEP 8, and is covered in the completion summary)
**STEP 7:** If manual mend, clippy, or doc issues found, execute <CreateBatchTodoList/>, <BatchDecisionPoint/>, <BatchExecution/>
**STEP 8:** Execute <RunFmt/> — run `~/.claude/scripts/lint/lint fmt`. Runs regardless of what earlier steps found; only `LINT_OP_FMT=off` skips it.
**STEP 9:** Completion summary
</ExecutionSteps>

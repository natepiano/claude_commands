<LoadStyleGuide>
Load the Rust style guide by running:
```bash
bash ~/.claude/scripts/load-rust-style.sh
```
Confirm using the script summary line. Then proceed.
</LoadStyleGuide>

<CheckCachedResults>
Run the cache check script:
```bash
bash ~/.claude/scripts/clippy/check_cache.sh .
```

- **Exit 0, all passed + "git diff: clean"**: Print the status table and exit (complete no-op).
- **Exit 0, all passed + "git diff: has changes"**: Print the status table, then skip to STEP 8 (<StyleReview/>) using the included diff.
- **Exit 0, issues found**: Print the status table and the `=== cargo mend ===` / `=== cargo clippy ===` details. Present issues as if mend + clippy had just run → skip to STEP 7 (<CreateBatchTodoList/>).
- **Exit 1**: Cache miss — proceed to STEP 2 (<RunMend/>).

The script reads Port Report's `latest.json`, waits if a run is still in progress, compares the cached timestamp to source files, and outputs formatted results.
</CheckCachedResults>

<RunMend>
Execute: `cargo mend`

Error Handling:
- **Environmental Issues (Stop execution):** If mend fails due to missing Cargo.toml or missing toolchain, inform user: "cargo mend cannot run - environment setup required." Then exit.

Analyze output:
- If mend reports **fixable items**, proceed to <RunMendFix/>.
- If mend reports **zero issues**, skip to <RunClippy/>.
- If mend reports **only unfixable items**, note them and skip to <RunClippy/>.
</RunMend>

<RunMendFix>
Execute: `cargo mend --fix`

Error Handling:
- **Any failure** (non-zero exit, error output, unexpected behavior): **STOP immediately**. Report the error output to the user and ask what to do. Do NOT proceed to clippy.

If successful: Note any remaining unfixable items from the earlier `cargo mend` run and proceed to <RunFmt/>.
</RunMendFix>

<RunFmt>
Execute: `cargo +nightly fmt --all`

This step runs **unconditionally** after mend (whether mend fixed anything, found nothing, or had only unfixable items).

Check `git diff` after running to determine if fmt applied any changes.
- If diff is non-empty: fmt applied formatting fixes. Note this for the findings report.
- If diff is empty: no formatting changes needed. Note this for the findings report.

Proceed to <RunClippy/>.
</RunFmt>

<RunClippy>
CLIPPY_FLAGS = --workspace --all-targets --all-features -- -D warnings

Execute: `cargo clippy ${CLIPPY_FLAGS} ${ARGUMENTS:-}`

If $ARGUMENTS provided, use as additional flags.
If different base configuration needed, user can override CLIPPY_FLAGS.

Error Handling:
- **Environmental Issues (Stop execution):** If clippy fails due to missing Cargo.toml, network issues, or missing toolchain, inform user: "Clippy cannot run - environment setup required. Check for Cargo.toml and valid Rust workspace." Then exit.
- **Compilation Errors (Process as todos):** If clippy fails due to compilation errors, treat these as high-priority todos alongside any warnings found.

Capture all output for analysis - both successful warnings and compilation errors become todos.
</RunClippy>

<ReportFindings>
Present a summary of all three steps before proceeding to todos:

## Findings
**Mend**: [one of: "Fixed N issues via `cargo mend --fix`" | "No issues found" | "No fixable issues found"] [if unfixable: "| N unfixable issues remaining"]
**Formatting**: [one of: "`cargo +nightly fmt` applied changes" | "No changes needed"]
**Clippy**: [one of: "N issues across M files" | "No issues found"]

Mend and fmt fixes are informational only — they are already applied, not action items.
Only unfixable mend issues and clippy issues become todos below.
</ReportFindings>

<CreateBatchTodoList>
Create a comprehensive todo list combining all clippy AND unfixable mend issues:
- Group related issues in same function/struct into single todos when logical
- Each todo includes fix description and affected file locations
- Label each todo with its source (clippy or mend) for clarity
- Present complete batch for user decision
- Note: Hook automatically provides cargo check feedback on edit - no explicit build commands needed
</CreateBatchTodoList>

<BatchDecisionPoint>
Present the complete batch of fixes:

## Issues Found
**Clippy**: [clippy_count] issues across [clippy_file_count] files
**Mend (unfixable)**: [mend_count] issues across [mend_file_count] files
[List all todos with descriptions, grouped by source]

## Available Actions
- **proceed** - Fix all issues using standard clippy guidance
- **change** - Modify the approach (specify changes)
- **stop** - Cancel fixes without making changes

Please select one of the keywords above.
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
</FixingGuidelines>

<StyleReview>
**This step runs unconditionally** — even if mend and clippy found zero issues.

1. Run `git diff` to see all uncommitted changes
2. If the diff is empty, report: "No uncommitted changes to review." and skip remaining steps.
3. Load the Rust style guide by running: `bash ~/.claude/scripts/load-rust-style.sh`
4. Evaluate every change against the loaded style guide rules
5. If any changes violate the style guide, fix them
6. If no violations found, report: "Style review passed — all changes conform to the style guide."
</StyleReview>

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <CheckCachedResults/> — check for fresh Port Report results. If fresh, skip to STEP 6 (passed) or STEP 5 (failed).
**STEP 2:** Execute <RunMend/> — run `cargo mend` to check for issues
**STEP 3:** If fixable items found, execute <RunMendFix/> — run `cargo mend --fix`. If it fails, STOP and ask user.
**STEP 4:** Execute <RunFmt/> — run `cargo +nightly fmt --all` unconditionally
**STEP 5:** Execute <RunClippy/>
**STEP 6:** Execute <ReportFindings/> — present mend, fmt, and clippy summary
**STEP 7:** If unfixable mend or clippy issues found, execute <CreateBatchTodoList/>, <BatchDecisionPoint/>, <BatchExecution/>
**STEP 8:** **Always** execute <StyleReview/> — evaluate diff against style guide rules (loads style guide only if diff is non-empty)
**STEP 9:** Completion summary
</ExecutionSteps>

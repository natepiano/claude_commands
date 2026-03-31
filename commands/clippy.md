<LoadStyleGuide>
Load the Rust style guide by running:
```bash
cat ~/rust/nate_style/rust/*.md
```
Confirm: "Rust style guide loaded." Then proceed.
</LoadStyleGuide>

<CheckCachedResults>
Check if the background lint watcher has fresh results before running mend + clippy.

1. Check if `target/port-report.log` exists in the current project. If not → skip to <RunMend/>.

2. Read the last line of the file. It will be in the format: `{ISO-8601-timestamp}\t{status}` where status is `started`, `passed`, or `failed`.

3. If the last line status is `started`:
   - Check the timestamp. If it's more than 30 minutes old, treat as stale → skip to <RunMend/>.
   - Otherwise, inform the user: "Lint watcher is running, waiting for results..."
   - Poll the file every 2 seconds (up to 5 minutes) until the last line changes to `passed` or `failed`.
   - If timeout → inform user and proceed to <RunMend/>.

4. If the last line status is `passed` or `failed`:
   - Find the newest `.rs` or `Cargo.toml` file in the project (excluding `target/`):
     ```bash
     find . -path ./target -prune -o \( -name '*.rs' -o -name 'Cargo.toml' \) -print0 | xargs -0 stat -f '%m %N' | sort -rn | head -1
     ```
   - Compare the file's mtime to the log entry timestamp. If any source file is newer than the log entry → results are stale, skip to <RunMend/>.

5. If results are fresh:
   - **passed**: Report "All lint checks passed (cached from {timestamp}). Skipping re-run." → skip to <StyleReview/>.
   - **failed**: Read `target/port-report/clippy-latest.log` and `target/port-report/mend-latest.log`. Present any issues found as if mend + clippy had just run → skip to <CreateBatchTodoList/>.
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

If successful: Note any remaining unfixable items from the earlier `cargo mend` run and proceed to <RunClippy/>.
</RunMendFix>

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
2. If the diff is empty, report: "No uncommitted changes to review." and skip.
3. Evaluate every change against the loaded style guide rules
4. If any changes violate the style guide, fix them
5. If no violations found, report: "Style review passed — all changes conform to the style guide."
</StyleReview>

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <LoadStyleGuide/> — load the Rust style guide
**STEP 2:** Execute <CheckCachedResults/> — check for fresh lint watcher results. If fresh, skip to STEP 7 (passed) or STEP 6 (failed).
**STEP 3:** Execute <RunMend/> — run `cargo mend` to check for issues
**STEP 4:** If fixable items found, execute <RunMendFix/> — run `cargo mend --fix`. If it fails, STOP and ask user.
**STEP 5:** Execute <RunClippy/> — Report: "Found [clippy_count] clippy issues and [mend_count] unfixable mend issues"
**STEP 6:** If issues found in steps 3-5, execute <CreateBatchTodoList/>, <BatchDecisionPoint/>, <BatchExecution/>
**STEP 7:** **Always** execute <StyleReview/> — evaluate the diff against style guide rules and fix any violations
**STEP 8:** Completion summary
</ExecutionSteps>

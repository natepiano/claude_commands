<AutoProceed>
If $ARGUMENTS contains the token `auto-proceed` (injected by /plan:delegate and
the codex work orders it composes), this run is non-interactive: strip the token
before any remaining arguments reach `lint clippy`, and <BatchDecisionPoint/>
reports the batch then immediately executes it as **proceed** — no stop, no user
wait. Auto-proceed does NOT soften the hard stops in <RunMendFix/> or the
environmental-failure handling anywhere in this skill — those still stop.
</AutoProceed>

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
bash ~/.claude/scripts/clippy/check_cache.sh .
```

- **Exit 0, all passed + "git diff: clean"**: Print the status table and exit (complete no-op).
- **Exit 0, all passed + "git diff: has changes"**: Print the status table, then resume at STEP 4 and continue through remaining steps in order (4 → 5 → 5b → 6 → 8 → 9).
- **Exit 0, issues found**: Print the status table and the `=== lint mend ===` / `=== lint clippy ===` / `=== lint doc ===` details. Then resume at STEP 7 and execute it in full — **including stopping at `<BatchDecisionPoint/>` and waiting for user approval before any edits** (in auto-proceed mode the gate reports and proceeds instead — see <AutoProceed/>). "Resuming at STEP 7" does not mean skipping the decision gate.
- **Exit 1**: Cache miss — proceed to STEP 2 (<RunMend/>).

The script reads lint-runs' `latest.json`, waits if a run is still in progress, compares the cached timestamp to source files, and outputs formatted results.
</CheckCachedResults>

<RunMend>
Execute: `~/.claude/scripts/clippy/lint mend`

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
Execute: `~/.claude/scripts/clippy/lint mend --fix`

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
    1. The full `~/.claude/scripts/clippy/lint mend --fix` output, including
       the lint(s) mend tried to fix and the compiler error(s) that triggered
       the revert.
    2. The exact command that reproduces it: `~/.claude/scripts/clippy/lint mend --fix`
    3. `git rev-parse HEAD` and `git status --short` output, confirming the
       working tree matches the reproduction state.
  Then end your turn and wait for the user. Do NOT proceed to clippy.
- **Any other failure** (non-zero exit, error output, unexpected behavior):
  **STOP immediately**. Report the error output to the user and ask what to do.
  Do NOT proceed to clippy.

If successful: Note any remaining unfixable items from the earlier `cargo mend` run, then continue with the next step in <ExecutionSteps/>.
</RunMendFix>

<RunFmt>
Execute: `~/.claude/scripts/clippy/lint fmt`

This step runs **unconditionally** (whether mend fixed anything, found nothing, or had only unfixable items).

Check `git diff` after running to determine if fmt applied any changes.
- If diff is non-empty: fmt applied formatting fixes. Note this for the completion summary.
- If diff is empty: no formatting changes needed. Note this for the completion summary.
</RunFmt>

<RunClippy>
The `lint` wrapper supplies `--workspace --all-targets --all-features -- -D warnings`.

Execute: `~/.claude/scripts/clippy/lint clippy ${ARGUMENTS:-}`

If $ARGUMENTS provided, use as additional flags — after removing the
`auto-proceed` token, which is a mode switch, not a clippy flag (see <AutoProceed/>).
If different base configuration needed, user can override CLIPPY_FLAGS.

Error Handling:
- **Environmental Issues (Stop execution):** If clippy fails due to missing Cargo.toml, network issues, or missing toolchain, inform user: "Clippy cannot run - environment setup required. Check for Cargo.toml and valid Rust workspace." Then exit.
- **Compilation Errors (Process as todos):** If clippy fails due to compilation errors, treat these as high-priority todos alongside any warnings found.

Capture all output for analysis - both successful warnings and compilation errors become todos.
</RunClippy>

<RunDoc>
Execute: `~/.claude/scripts/clippy/lint doc`

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
Present a summary before proceeding to todos:

## Findings
**Mend**: [one of: "Fixed N issues via `~/.claude/scripts/clippy/lint mend --fix`" | "No issues found" | "No fixable issues found"] [if unfixable: "| N unfixable issues remaining"]
**Style review**: [one of: "N violations fixed" | "All changes conform" | "No uncommitted changes to review"]
**Clippy**: [one of: "N issues across M files" | "No issues found"]
**Doc**: [one of: "N rustdoc errors across M files" | "No issues found"]

Mend and style fixes are already applied, not action items.
Only unfixable mend issues, clippy issues, and rustdoc errors become todos below.
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

Present the complete batch of fixes exactly as follows:

## Issues Found
**Clippy**: [clippy_count] issues across [clippy_file_count] files
**Doc**: [doc_count] rustdoc errors across [doc_file_count] files
**Mend (unfixable)**: [mend_count] issues across [mend_file_count] files

For each todo, show:
- file:line and lint name
- the fix approach in one sentence
- `rule:` with the matched style-guide filename from the `lint:`
  frontmatter lookup, when a match exists (omit the field otherwise)

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
**This step runs unconditionally** — even if mend and clippy found zero issues.

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
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <CheckCachedResults/> — check for fresh lint-runs results. Follow its resume instructions (may skip ahead but always continues through remaining steps in order).
**STEP 2:** Execute <RunMend/> — run `~/.claude/scripts/clippy/lint mend` to check for issues
**STEP 3:** If fixable items found, execute <RunMendFix/> — run `~/.claude/scripts/clippy/lint mend --fix`. If it fails, STOP and ask user.
**STEP 4:** **Always** execute <StyleReview/> — evaluate diff against style guide rules (loads style guide only if diff is non-empty)
**STEP 5:** Execute <RunClippy/>
**STEP 5b:** Execute <RunDoc/> — run rustdoc as a lint with `~/.claude/scripts/clippy/lint doc`
**STEP 6:** Execute <ReportFindings/> — present mend, clippy, and doc summary (fmt runs later, at STEP 8, and is covered in the completion summary)
**STEP 7:** If unfixable mend or clippy issues found, execute <CreateBatchTodoList/>, <BatchDecisionPoint/>, <BatchExecution/>
**STEP 8:** Execute <RunFmt/> — run `~/.claude/scripts/clippy/lint fmt` unconditionally
**STEP 9:** Completion summary
</ExecutionSteps>

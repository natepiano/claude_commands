# Codex Review

**Purpose:** Run a code review via Codex CLI (OpenAI) on the current working tree changes, then synthesize the findings with your own analysis.

**Usage:** `/codex_review <mode> [options]`

**Arguments:**
- $ARGUMENTS (required): Review mode and options.
  - `uncommitted` — review staged, unstaged, and untracked changes
  - `base <branch>` — review changes against a base branch (e.g. `base main`)
  - `file <path> [base_branch]` — review changes to a specific file (optionally against a base branch)
  - Any remaining text after the mode keyword(s) is passed as custom review instructions to Codex

SESSION_DIR = /tmp/claude/codex_review
SCRIPT_PATH = ~/.claude/scripts/codex_review/codex_review.sh

---

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <ParseArguments/>
**STEP 2:** Execute <PrepareSession/>
**STEP 3:** Execute <LaunchCodexReview/>
**STEP 4:** Execute <WaitForBashNotification/>
**STEP 5:** Execute <ReadDiffContext/>
**STEP 6:** Execute <SynthesizeReview/>
**STEP 7:** Execute <StructureFindings/>
**STEP 8:** Execute <FindingsSummaryTable/> from @~/.claude/shared/findings_walkthrough.md
**STEP 9:** Execute <FindingsWalkthrough/> from @~/.claude/shared/findings_walkthrough.md
**STEP 10:** Execute <FindingsCompletion/> from @~/.claude/shared/findings_walkthrough.md
</ExecutionSteps>

---

<PrepareSession>
**Goal:** Create a clean session directory.

1. Run: `rm -rf /tmp/claude/codex_review && mkdir -p /tmp/claude/codex_review`
2. Identify the current working directory (the project the user is working in)
4. Store as ${WORKING_DIR}
</PrepareSession>

---

<ParseArguments>
**Goal:** Determine review mode and custom prompt from $ARGUMENTS.

**If $ARGUMENTS is empty**, display usage and stop:

```
## Codex Review — Usage

/codex_review uncommitted              — review staged, unstaged, and untracked changes
/codex_review base <branch>            — review changes against a base branch
/codex_review file <path>              — review uncommitted changes to a specific file
/codex_review file <path> <branch>     — review changes to a file against a base branch

Optional: append custom instructions after the mode, e.g.:
/codex_review uncommitted Focus on error handling
```

**Stop execution after displaying usage.**

**Parsing rules (when $ARGUMENTS is provided):**

1. **If $ARGUMENTS starts with `uncommitted`:** Set ${MODE} = `uncommitted`, remainder is ${CUSTOM_PROMPT}
2. **If $ARGUMENTS starts with `base`:** Set ${MODE} = `base`, next word is ${MODE_ARG} (the branch name), remainder is ${CUSTOM_PROMPT}
3. **If $ARGUMENTS starts with `file`:** Set ${MODE} = `file`, next word is ${MODE_ARG} (the file path), next word (if present) is ${CUSTOM_PROMPT} (used as base branch by the script)
4. **If $ARGUMENTS doesn't match any mode keyword:** Set ${MODE} = `uncommitted`, entire $ARGUMENTS is ${CUSTOM_PROMPT}

**Inform the user** what will be reviewed:
- For uncommitted: "Reviewing uncommitted changes via Codex..."
- For base: "Reviewing changes against ${MODE_ARG} via Codex..."
- For file: "Reviewing changes to ${MODE_ARG} via Codex..."
- If custom prompt (non-file modes): append "Focus: ${CUSTOM_PROMPT}"
</ParseArguments>

---

<LaunchCodexReview>
**Goal:** Launch the review in the background.

1. Run the script using the Bash tool with `run_in_background: true` and `dangerouslyDisableSandbox: true` (Codex CLI requires unsandboxed access to macOS system APIs):
   ```
   bash ~/.claude/scripts/codex_review/codex_review.sh "/tmp/claude/codex_review" "${WORKING_DIR}" "${MODE}" "${MODE_ARG}" "${CUSTOM_PROMPT}"
   ```
2. Inform the user: "Codex is reviewing... I'll read the same diff while we wait."
</LaunchCodexReview>

---

<WaitForBashNotification>
**Goal:** Wait for Codex to finish.

The background Bash task launched in <LaunchCodexReview/> will automatically send a task notification when it completes. **Do NOT poll the status file in a loop.** Simply proceed to <ReadDiffContext/> and work on reading the diff. The notification will arrive on its own.

**When the task notification arrives:**
1. **If status is "completed":** Read `/tmp/claude/codex_review/status` to confirm, then read `/tmp/claude/codex_review/review.txt` — proceed to <SynthesizeReview/>
2. **If status is "failed" or "killed":** Read `/tmp/claude/codex_review/codex.log` for diagnostics. Inform the user and stop execution.
</WaitForBashNotification>

---

<ReadDiffContext>
**Goal:** Read the same diff that Codex reviewed so you can form your own opinion.

**While waiting for the background task (or after it completes):**

- For `uncommitted` mode: Run `git diff` and `git diff --staged` in ${WORKING_DIR}
- For `base` mode: Run `git diff ${MODE_ARG}...HEAD` in ${WORKING_DIR}
- For `file` mode: Read the file at ${MODE_ARG} directly. If a base branch was given, run `git diff <branch>...HEAD -- ${MODE_ARG}` in ${WORKING_DIR}

Skim the diff to understand what changed. You do not need to analyze exhaustively — focus on the areas Codex flagged so you can agree or push back.
</ReadDiffContext>

---

<SynthesizeReview>
**Goal:** Present Codex's review findings alongside your own analysis.

**Format:**

```
## Codex Review

[Codex's findings, faithfully presented. Preserve their structure — if they used
categories or severity levels, keep them. Quote specific findings directly.]

## My Analysis

[Your own review of the same diff. Call out:
- Findings you agree with and why
- Findings you disagree with and why
- Anything Codex missed that you noticed
- Anything Codex caught that you wouldn't have flagged]

## Action Items

[Concrete list of what the user should address, synthesized from both reviews.
Order by severity. If both reviewers agree on an issue, note the consensus.
If they disagree, present both perspectives and your recommendation.]
```

**Principles:**
- Codex's findings come first — the user invoked this to get a second opinion
- Be specific about agreement and disagreement
- Action items should be actionable, not vague
- If the diff is clean and both reviewers agree, say so briefly
</SynthesizeReview>

---

<StructureFindings>
**Goal:** Convert the synthesized review into ${FINDINGS_LIST} format for the shared walkthrough.

Take the Action Items from <SynthesizeReview/> and decompose them into individual findings:

1. For each action item, create a finding with:
   - `id`: Sequential (F1, F2, F3...)
   - `title`: Brief name for the issue
   - `severity`: critical / important / minor — based on the consensus between Codex and Claude
   - `problem`: The full issue description, including relevant code context
   - `impact`: Why it matters
   - `recommendation`: The specific actionable suggestion
   - `source`: "Codex", "Claude", or "Both" depending on who identified it

2. Order by severity: critical first, then important, then minor

3. Store as ${FINDINGS_LIST}

4. Set ${SOURCE_SUMMARY} = "Reviewed by Codex and Claude"
5. Set ${REVIEW_TOPIC} = description of what was reviewed (e.g. "uncommitted changes", "changes against main")

**If the diff is clean** and both reviewers agree there are no issues, skip the walkthrough entirely and end with the synthesis output.
</StructureFindings>

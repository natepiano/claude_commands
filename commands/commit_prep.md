**IMPORTANT** don't commit the changes that you will examine. Just do the following:

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    **STEP 1:** Execute <AnalyzeChanges/>
    **STEP 2:** Execute <CommitTitleHandling/>
    **STEP 3:** Execute <GenerateCommitBody/>
    **STEP 4:** Execute <FinalCommitDecision/>
</ExecutionSteps>

<AnalyzeChanges>
Run `bash ~/.claude/scripts/commit_prep/analyze_changes.sh` to gather git status and diffs in a single command.
**CRITICAL**: You must evaluate EVERY uncommitted file for inclusion in the commit — regardless of file type (.md, .yml, .toml, .rs, etc.) and regardless of what task you were working on prior to this command. Do not carry over any file exclusions from previous operations. The commit candidate set is determined solely by `git status`, not by what you were previously focused on.
If the script reports no uncommitted changes, inform the user and stop.
</AnalyzeChanges>

<CommitTitleHandling>
If $ARGUMENTS is provided:
- Use $ARGUMENTS as the commit title

If no $ARGUMENTS provided:
- Suggest a concise conventional commit title (one line, under 72 characters)
- Execute <UserTitleConfirmation/>
</CommitTitleHandling>

<UserTitleConfirmation>
Present to user:

## Available Actions
- **use** - Use the suggested commit title
- **change** - Provide a different commit title

Wait for user response.

If user selects **change**: Ask for new title and use their provided title.
If user selects **use**: Use the suggested title.
</UserTitleConfirmation>

<GenerateCommitBody>
Using the analyzed changes and established commit title, generate a full conventional commit message:
- Commit body: 10-15 lines of high-level bullet points covering what changed, why, and what it affects
- Do not end with flowery summary statements that editorialize the change (e.g., "This improves maintainability and makes the codebase cleaner") — just state the facts
- Avoid exhaustive details or deep subsections

Present the full commit message to the user:

```
**Proposed commit message:**
[full commit message]
```
</GenerateCommitBody>

<FinalCommitDecision>
Present to user:

## Available Actions
- **commit** - Execute the git commit with the prepared message
- **abandon** - Stop without committing

Wait for user response.

If user selects **commit**:

Pick the path for your agent:

**If you are Claude (Bash tool):**
- Stage files with `git add <paths>` (one or more explicit paths — do not use `git add -A` or `git add .`).
- Commit directly with `git commit -m "$(cat <<'EOF' ... EOF)"` using a quoted heredoc for the multi-line message. `Bash(git add *)` and `Bash(git commit *)` are in the user allowlist, so this runs without a secondary permission prompt.
- Do NOT use the helper scripts (`create_message_file.sh`, `stage_and_commit.sh`). They exist for Codex's sandbox model and only add a permission prompt when invoked from Claude, because the shell wrapper (`MSG_FILE=$(...)` or `bash <script>`) prevents the allowlist prefix match from firing.

**If you are Codex (or another agent whose sandbox blocks `git add` / `git commit` from writing `.git/index.lock`):**
- Write the prepared commit message to a system temp file via
  `~/.claude/scripts/commit_prep/create_message_file.sh`. The helper reads the
  message from **stdin** and prints the temp file path to stdout when
  `--stdout-path` is passed:

  ```bash
  MSG_FILE=$(~/.claude/scripts/commit_prep/create_message_file.sh --stdout-path <<'EOF'
  <title line>

  <body line 1>
  <body line 2>
  ...
  EOF
  )
  ```

  Notes:
  - Never run the helper with no stdin — it exits 1 with "commit message is empty".
  - Keep the heredoc delimiter quoted (`<<'EOF'`) so `$`, backticks, and backslashes are preserved literally.

- Then run the stage+commit helper, passing the captured path:

  ```bash
  bash ~/.claude/scripts/commit_prep/stage_and_commit.sh "$MSG_FILE"
  ```

  Request escalation with `sandbox_permissions: "require_escalated"` (or equivalent) so the helper can write `.git/index.lock`. Do not fail the workflow on the first sandbox denial — retry once with escalation.

Then execute <CommitOutput/>

If user selects **abandon**: Run `git reset` to unstage any changes (if staged) and stop
</FinalCommitDecision>

<CommitOutput>
Format output as:

```
**Commit successful**

**Commit hash**: `[short hash]`
**Changes**: [files changed summary]
```

**Formatting requirements**:
- Each field on its own line
- Commit hash in code backticks
- Blank line between commit info and additional status
</CommitOutput>

**IMPORTANT** don't commit the changes that you will examine. Just do the following:

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    **STEP 1:** Execute <AnalyzeChanges/>
    **STEP 2:** Execute <ClippyPrecheck/>
    **STEP 3:** Execute <CommitTitleHandling/>
    **STEP 4:** Execute <GenerateCommitBody/>
    **STEP 5:** Execute <FinalCommitDecision/>
</ExecutionSteps>

<ClippyPrecheck>
If `$ARGUMENTS` contains the token `noclippy` (case-insensitive, matched as a whole word), skip this step silently. Strip the `noclippy` token from `$ARGUMENTS` before any later step uses it (so it is not treated as part of the commit title).

If `$ARGUMENTS` contains the token `clippy` (case-insensitive, matched as a whole word — and not `noclippy`), invoke the `clippy` skill immediately without asking, then continue to the next step. Strip the `clippy` token from `$ARGUMENTS` before any later step uses it (so it is not treated as part of the commit title).

First, look at the file list from <AnalyzeChanges/>. If none of the uncommitted files are Rust source (`.rs`) or Cargo manifests (`Cargo.toml`, `Cargo.lock`), skip this step silently — clippy is irrelevant.

Otherwise, check your own conversation context to determine whether `/clippy` has been run recently — i.e., whether the `clippy` skill has been invoked in this session **after the most recent Rust code changes**.

- **If `/clippy` has been run recently after the latest Rust edits**: Proceed silently to the next step. Do NOT ask the user. Do NOT mention clippy. Do not take an extra turn.
- **If `/clippy` has NOT been run recently** (or no clippy run is visible in context at all): Ask the user exactly once:

  > Run `/clippy` first? (yes/no)

  Wait for the user's response.
  - If **yes**: Invoke the `clippy` skill, then continue to the next step.
  - If **no**: Continue to the next step without running clippy.
</ClippyPrecheck>

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
Using the analyzed changes and established commit title, generate a full conventional commit message.

Use only as many bullets as the commit needs — a small or single-purpose change may need one bullet or none; a large change may need several.

Every bullet must carry information a reviewer cannot get from the diff or the title. Fold the "why" into the bullet it explains rather than adding a separate rationale line.

Do NOT include:
- Bullets stating what did NOT change ("keep X unchanged", "preserve all call sites", "logic identical") — omission already implies this
- Meta-commentary about the commit itself ("limit to a single file for focused review", "leave follow-up for future work", "make the diff a pure ordering change")
- Restatements of the title in other words
- Flowery or editorializing summaries (e.g., "This improves maintainability and makes the codebase cleaner") — state facts only

If the change is purely mechanical (rename, reorder, move) with no behavior change, say so in one bullet and stop.

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

**If you are Codex:**
- Write the prepared commit message to a system temp file first. Do this without escalation. Keep the commit message out of the permission request and out of the `git commit` command line.

  ```bash
  cat >/tmp/commit-prep-message.txt <<'EOF'
  <title line>

  <body line 1>
  <body line 2>
  ...
  EOF
  ```

- Stage files with direct, explicit-path git commands. Do not use `git add -A` or `git add .`.

  ```bash
  git add -- <path> <path> ...
  ```

- Commit directly with `git commit -F <temp-message-file>`.

  ```bash
  git commit -F /tmp/commit-prep-message.txt
  ```

- Do not use `~/.claude/scripts/commit_prep/stage_and_commit.sh` by default. It stages with `git add -A`, which can widen the commit scope, and invoking it through `bash` can turn the helper invocation into a one-off permission request.
- Do not request escalation preemptively. Only if direct `git add -- <paths>` or direct `git commit -F <temp-message-file>` fails because Codex cannot write `.git/index.lock` or another git metadata path, retry the same direct git command once with `sandbox_permissions: "require_escalated"`. Use a stable prefix such as `git add` or `git commit`; never put the full commit message in the escalated command text.

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

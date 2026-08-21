---
description: Analyze staged and unstaged changes and present a complete commit message for a single approve-or-abandon decision; runs clippy only when the `clippy` token is passed.
---

**IMPORTANT** don't commit the changes that you will examine. Just do the following:

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    **STEP 1:** Execute <AnalyzeChanges/>
    **STEP 2:** Execute <ClippyPrecheck/>
    **STEP 3:** Execute <DraftCommitMessage/>
    **STEP 4:** Execute <CommitDecision/>
</ExecutionSteps>

There is exactly ONE approval gate, in <CommitDecision/>. Never stop to confirm
the title on its own — the title and body are presented together, once.

<ClippyPrecheck>
**Clippy is opt-in, and the default is not to run it.** Skip this step
silently unless explicitly asked: no clippy run, no question about running
one, no mention of it in your output. Never ask "Run `/clippy` first?" — the
answer is already no unless the token below is present.

If `$ARGUMENTS` contains the token `clippy` (case-insensitive, matched as a whole word — and not `noclippy`), invoke the `clippy` skill immediately without asking, then continue to the next step.

Strip both the `clippy` and `noclippy` tokens from `$ARGUMENTS` before any later step uses it, so neither is treated as part of the commit title. `noclippy` is accepted and ignored — it names what already happens by default.

When the `clippy` skill runs (from here or otherwise), its `<StyleReview/>` step must be done inline, not delegated to a subagent — the user needs to track progress through the whole workflow, not wait on an unauditable subagent turn.
</ClippyPrecheck>

<AnalyzeChanges>
Run `bash ~/.claude/scripts/commit_prep/analyze_changes.sh` to gather git status and diffs in a single command.
**CRITICAL**: You must evaluate EVERY uncommitted file for inclusion in the commit — regardless of file type (.md, .yml, .toml, .rs, etc.) and regardless of what task you were working on prior to this command. Do not carry over any file exclusions from previous operations. The commit candidate set is determined solely by `git status`, not by what you were previously focused on.
If the script reports no uncommitted changes, inform the user and stop.
</AnalyzeChanges>

<DraftCommitMessage>
Write the complete conventional commit message — title and body together —
without pausing for approval of either part.

**Title**: if $ARGUMENTS is non-empty after the token stripping in
<ClippyPrecheck/>, use it verbatim as the title. Otherwise compose one: a
concise conventional commit title, one line, under 72 characters.

**Body**: generate it from the analyzed changes and the established title.

Use only as many bullets as the commit needs — a small or single-purpose change may need one bullet or none; a large change may need several.

Every bullet must carry information a reviewer cannot get from the diff or the title. Fold the "why" into the bullet it explains rather than adding a separate rationale line.

Do NOT include:
- Bullets stating what did NOT change ("keep X unchanged", "preserve all call sites", "logic identical") — omission already implies this
- Meta-commentary about the commit itself ("limit to a single file for focused review", "leave follow-up for future work", "make the diff a pure ordering change")
- Restatements of the title in other words
- Flowery or editorializing summaries (e.g., "This improves maintainability and makes the codebase cleaner") — state facts only

If the change is purely mechanical (rename, reorder, move) with no behavior change, say so in one bullet and stop.

Present the full commit message to the user, title line included:

```
**Proposed commit message:**
[full commit message]
```

Then go straight to <CommitDecision/> in the same turn.
</DraftCommitMessage>

<CommitDecision>
Present to user, immediately below the proposed message:

## Available Actions
- **commit** - Execute the git commit with the prepared message
- **change** - Say what to change (title, body, or file selection); revise and re-present
- **abandon** - Stop without committing

Wait for user response.

If user selects **change**: apply what they asked for, present the revised
message in full, and return to this same gate. Do not split the revision into
separate title and body approvals.

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
- For all repos, if direct `git add -- <paths>` or direct `git commit -F <temp-message-file>` fails because Codex cannot write `.git/index.lock` or another git metadata path, retry that exact command once using escalated execution.
  - Use a stable `git add` prefix for staging retries.
  - Use a stable `git commit` prefix for commit retries.
  - Never include the full commit message in an escalated command.

Then execute <CommitOutput/>

If user selects **abandon**: Run `git reset` to unstage any changes (if staged) and stop
</CommitDecision>

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

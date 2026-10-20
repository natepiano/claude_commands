---
description: Suggest and create a git worktree and branch, and copy local settings into it.
---

Use TodoWrite tool to create initial todos:
- "Suggest worktree and branch name to user"
- "Get user approval for worktree creation"
- "Create approved worktree and branch"
- "Allow the worktree's .envrc so direnv-gated tooling works"

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <SuggestWorktreeName/>
**STEP 2:** Execute <GetUserApproval/>
**STEP 3:** Execute <CreateWorktree/>
**STEP 4:** Execute <CopySettingsLocal/>
**STEP 5:** Execute <AllowDirenv/>
</ExecutionSteps>

<SuggestWorktreeName>
Mark first todo as in_progress.

**Validate Git Repository:**
- Use Bash tool to run `git rev-parse --show-toplevel` to confirm we're in a git repository
- If not in git repository, display 'Not in git repository' and STOP

**Process Arguments and Generate Name:**
- If $ARGUMENTS provided and not empty: use $ARGUMENTS as suggested worktree name
- If $ARGUMENTS empty:
  - Use Bash tool to run `git branch --show-current` to get current branch name
  - Generate worktree name suggestion using format: `worktree-[current-branch]-[timestamp]`

**Check for Conflicts:**
- Use Bash tool to run `git worktree list` to check existing worktrees
- If suggested name already exists as worktree, append `-2`, `-3`, etc. until unique name found
- Validate name doesn't contain invalid characters (/, \, :, *, ?, ", <, >, |)
- If invalid characters found, display 'Invalid characters in worktree name' and ask user for different name

**Present Suggestion:**
- Present suggestion to user: "I suggest creating worktree: '[suggested-name]' with branch '[branch-name]'. Do you approve this name or would you like to use a different one?"

Mark first todo as completed when suggestion is presented.
</SuggestWorktreeName>

<GetUserApproval>
Mark second todo as in_progress. Present the suggestion with proper keyword format:

## Available Actions
- **approve** - Accept the suggested worktree name and proceed
- **modify** - Provide a different name for the worktree
- **stop** - Exit without creating worktree

Please select one of the keywords above.

STOP and wait for user response. If user provides alternative name, use their name. If user approves, use suggested name. Mark second todo as completed after receiving user confirmation.
</GetUserApproval>

<CreateWorktree>
Mark third todo as in_progress.

**Create Worktree:**
- Use Bash tool to run `git worktree add ../[worktree-name] -b [branch-name]` to create worktree and branch
- If command fails, display error message and ask user for guidance

**Verify (without changing directory):**
- Confirm success by running `git -C ../[worktree-name] branch --show-current` to verify the branch
- Do NOT cd into the new worktree — stay in the current working directory
- Inform user: "Worktree '[worktree-name]' created at ../[worktree-name] on branch '[branch-name]'. You remain in [current-directory]."

Mark third todo as completed when finished.
</CreateWorktree>

<CopySettingsLocal>
**Copy settings.local.json to the new worktree:**

- Run `bash ~/.claude/scripts/make_a_worktree/copy_settings_local.sh ../[worktree-name]`
- Inform user: "Copied settings.local.json to worktree."
</CopySettingsLocal>

<AllowDirenv>
**Approve the worktree's `.envrc` so direnv-gated tooling works there.**

A fresh worktree carries the repo's `.envrc` but not direnv's approval, so the
environment stays blocked. Anything that shells through `direnv exec` then fails
without ever touching the code — cargo-port, for one, reports the lint column as
environment-unavailable (yellow) rather than pass/fail.

- Run `bash ~/.claude/scripts/make_a_worktree/direnv_allow.sh ../[worktree-name]`
- If the output says there is no `.envrc`, SKIP silently — nothing to report.
- Otherwise report the result in one line: allowed, plus whether the environment
  probe passed. If the probe still fails, surface the script's warning so the
  user knows lints in that worktree will not run yet.
</AllowDirenv>

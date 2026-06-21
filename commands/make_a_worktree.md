Use TodoWrite tool to create initial todos:
- "Suggest worktree and branch name to user"
- "Get user approval for worktree creation"
- "Create approved worktree and branch"
- "Offer clean-fix eval/fix redirect if the worktree matches a project"

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <SuggestWorktreeName/>
**STEP 2:** Execute <GetUserApproval/>
**STEP 3:** Execute <CreateWorktree/>
**STEP 4:** Execute <CopySettingsLocal/>
**STEP 5:** Execute <OfferCleanFixRedirect/>
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

<OfferCleanFixRedirect>
**Offer to point clean-fix's style eval/fix at this worktree.**

clean-fix evaluates/fixes a fixed allowlist of projects (`[projects]` in
`~/.claude/scripts/clean-fix/clean-fix.conf`). When a worktree is a checkout of
one of those projects, you usually want the eval/fix work to follow the worktree
while the project's identity/history stays put. This step offers that.

**Guard — only proceed if the worktree is a sibling under `~/rust`:**
- The worktree was created at `../[worktree-name]`. Resolve its parent.
- If the parent is not `~/rust` (i.e. `$HOME/rust`), SKIP this step silently — clean-fix paths are relative to `~/rust`, so a redirect would be invalid. Do not mention it.

**Detect a match:**
- Get the primary repo name: `basename "$(git rev-parse --show-toplevel)"`.
- Run:
  `python3 ~/.claude/scripts/make_a_worktree/retarget_clean_fix.py detect --repo [repo-name] --worktree [worktree-name]`
- If the JSON has `"match": false`, SKIP silently — this worktree's name is not prefixed by the repo or one of its `[projects]` member crates, so there's nothing to redirect. Do not mention it.

**On a match, ask the user (do NOT auto-apply):**
- Present the redirect concisely, e.g.:
  > Worktree `[worktree-name]` matches clean-fix project(s) `[redirects[].entry]`. Point style eval/fix at this worktree (and add it to the nightly build set)? The project keeps its name and history.
- Offer keywords: **approve** / **skip**. STOP and wait.

**On approve:**
- Run:
  `python3 ~/.claude/scripts/make_a_worktree/retarget_clean_fix.py apply --repo [repo-name] --worktree [worktree-name]`
- This adds `[worktree-name]` to `[build]` and writes the `[active_checkout]` redirect(s); the `[projects]` lines are untouched, so history continuity is preserved. No restart needed — the clean-fix jobs read the conf live.
- Report the edits from the JSON (`redirects`, `build_add`).
- Note to the user: to undo later (e.g. after merging/deleting the worktree), `/worktree_delete` reverts the redirect automatically, or run the helper's `revert --worktree [worktree-name]`.

**On skip:** do nothing further.
</OfferCleanFixRedirect>

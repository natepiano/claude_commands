<Persona>
@~/.claude/shared/personas/git_expert_persona.md
</Persona>

I'll discover available worktrees and help you safely merge changes from another worktree.

SELECTED_WORKTREE = [User selected worktree path from step 1]
SOURCE_BRANCH = [Branch name from selected worktree]

First, create a todo list to track our progress using the TodoWrite tool:
- "Discover and validate worktree options"
- "Get user worktree selection"
- "Validate working tree status"
- "Sync with remote if needed"
- "Validate source worktree"
- "Test merge feasibility"
- "Perform actual merge"

Mark each todo as "in_progress" when starting that step, and "completed" when finished.

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 0:** Execute <Persona/> to adopt the Git Expert persona
    **STEP 1:** Execute <DiscoverWorktreeOptions/>
    **STEP 2:** Execute <CheckWorkingTreeStatusStep/>
    **STEP 3:** Execute <SyncWithRemote/>
    **STEP 4:** Execute <ValidateSourceWorktree/>
    **STEP 5:** Execute <TestMergeFeasibility/>
    **STEP 6:** Execute <PerformActualMerge/>
</ExecutionSteps>

<DiscoverWorktreeOptions>
    - Execute <ValidateGitRepo/>
    - Execute <GetCurrentBranch/>
    - Run `pwd` to show current working directory
    - Run `git worktree list` to show all worktrees
    - Parse the output to identify other worktrees (excluding current one)
    - Display current worktree and available source worktrees clearly

## Available Actions
- **select** - Choose a worktree to merge from by entering its path or number
- **cancel** - Exit without performing merge

Please select one of the keywords above.

[STOP and wait for user response]

**CAPTURE USER SELECTION:** Store the user's response as SELECTED_WORKTREE variable
**VALIDATE USER INPUT:** Verify SELECTED_WORKTREE matches one of the available worktree paths from `git worktree list`
If user input doesn't match available worktrees, display error and re-ask
</DiscoverWorktreeOptions>

<CheckWorkingTreeStatusStep>
    - Execute <CheckWorkingTreeStatus/>
    - If there are uncommitted changes, STOP and ask user to commit or stash first
</CheckWorkingTreeStatusStep>

<SyncWithRemote>
    - Check if 'origin' remote exists with `git remote get-url origin`
    - If origin exists, run `git fetch origin` to get latest remote changes
    - Check if current branch has upstream with `git rev-parse --abbrev-ref @{upstream}`
    - If has upstream, check if behind with `git rev-list HEAD..@{upstream} --count`
    - If behind remote, STOP and ask user if they want to pull first
    - If no origin remote, skip remote sync steps and continue
</SyncWithRemote>

<ValidateSourceWorktree>
    - Check if selected worktree path exists: `test -d "${SELECTED_WORKTREE}"`
    - Execute <ValidateGitRepo REPO_PATH="${SELECTED_WORKTREE}"/>
    - Execute <GetCurrentBranch REPO_PATH="${SELECTED_WORKTREE}"/> and store result as SOURCE_BRANCH
    - Execute <CheckWorkingTreeStatus REPO_PATH="${SELECTED_WORKTREE}"/>
    - If source has uncommitted changes, STOP and inform user
</ValidateSourceWorktree>

<TestMergeFeasibility>
    - Run `git merge --no-commit --no-ff ${SOURCE_BRANCH}` where SOURCE_BRANCH is from ValidateSourceWorktree
    - If merge would have conflicts, run `git merge --abort` and STOP to ask user
    - If no conflicts, run `git merge --abort` to undo test merge
</TestMergeFeasibility>

<PerformActualMerge>
    - Run `git merge ${SOURCE_BRANCH} -m "Merge branch '${SOURCE_BRANCH}' from worktree ${SELECTED_WORKTREE}"`
    - Report success to user
</PerformActualMerge>

<ValidateGitRepo>
**Parameters:** REPO_PATH (optional, defaults to current directory)
**Purpose:** Verify location is a valid git repository
- If REPO_PATH provided: Run `git -C ${REPO_PATH} rev-parse --show-toplevel`
- If REPO_PATH not provided: Run `git rev-parse --show-toplevel`
- If command fails, STOP and inform user location is not a git repository
</ValidateGitRepo>

<GetCurrentBranch>
**Parameters:** REPO_PATH (optional, defaults to current directory)
**Purpose:** Get current branch name from git repository
- If REPO_PATH provided: Run `git -C ${REPO_PATH} branch --show-current`
- If REPO_PATH not provided: Run `git branch --show-current`
- Store result as CURRENT_BRANCH variable
</GetCurrentBranch>

<CheckWorkingTreeStatus>
**Parameters:** REPO_PATH (optional, defaults to current directory)
**Purpose:** Check for uncommitted changes in working tree
- If REPO_PATH provided: Run `git -C ${REPO_PATH} status --porcelain`
- If REPO_PATH not provided: Run `git status --porcelain`
- If output is not empty, repository has uncommitted changes
</CheckWorkingTreeStatus>

HAPPY PATH: If all validations pass, proceed with merge without stopping
UNHAPPY PATH: Stop and ask user for guidance if:
- Not in a git worktree
- Have uncommitted changes
- Behind remote
- Source worktree invalid or has uncommitted changes
- Merge would have conflicts

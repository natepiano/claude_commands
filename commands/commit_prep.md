**IMPORTANT** don't commit the changes that you will examine. Just do the following:

<CommitTitleHandling>
If $ARGUMENTS is provided:
- Use $ARGUMENTS as the commit title (one-liner)
- Proceed directly to <CommitPrep>

If no $ARGUMENTS provided:
- First run `git status` and `git diff` to understand the changes
- Suggest a commit header based on the changes
- Ask user: "Use this commit title or would you like to change it?"
- Wait for user response
- If user responds with "use" followed by text (e.g., "use 'new title'" or "use: new title"):
  - Extract the new title from their response
  - Use that as the commit title
  - Proceed to <CommitPrep>
- If user confirms the suggested title, proceed to <CommitPrep>
</CommitTitleHandling>

<CommitPrep>
- run `git status` to ensure you're within a git repository that has uncommitted changes
- create a full commit message using the established commit title
- stage the changes with `git add`
- STOP - do not run `git commit`
- Show the user the staged changes and proposed commit message
</CommitPrep>

**CRITICAL** Never run `git commit` - only prepare and show the commit message to the user

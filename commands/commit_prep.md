**IMPORTANT** don't commit the changes that you will examine. Just do the following:

<CommitTitleHandling>
If $ARGUMENTS is provided:
- Use $ARGUMENTS as the commit title (one-liner)
- Proceed directly to <CommitPrep>

If no $ARGUMENTS provided:
- First run `git status` and `git diff` to understand the changes
- Suggest a commit header based on the changes
- Ask user: "Use this commit title or would you like to change it?"
- Wait for user response before proceeding
- Once commit title is confirmed, proceed to <CommitPrep>
</CommitTitleHandling>

<CommitPrep>
- run `git status` to ensure you're within a git repository that has uncommitted changes
- create a commit message using the established commit title
- stage the changes
- STOP - do not commit the changes
</CommitPrep>

**CRITICAL** don't commit the changes yet - just show the commit message you've created to the user

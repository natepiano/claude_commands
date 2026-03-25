**Arguments**: $ARGUMENTS (filename to exclude, e.g. `plan.md` — if empty, infer from the most recently written plan file in this conversation)

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    **STEP 1:** Determine the filename from $ARGUMENTS. If $ARGUMENTS is empty, infer the filename from the plan you most recently created or discussed in this conversation. If you cannot infer it, ask the user.
    **STEP 2:** Run `bash ~/.claude/scripts/exclude/exclude.sh <filename>` which handles: ensuring `.git/info/exclude` exists, checking for duplicates, removing from git tracking if needed, and appending to the exclude file.
    **STEP 3:** Report the script output to the user.
</ExecutionSteps>

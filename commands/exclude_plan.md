**Arguments**: $ARGUMENTS (plan filename, e.g. `plan.md` — if empty, infer from the most recently written plan file in this conversation)

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    **STEP 1:** Determine the plan filename from $ARGUMENTS. If $ARGUMENTS is empty, infer the filename from the plan you most recently created or discussed in this conversation. If you cannot infer it, ask the user.
    **STEP 2:** Ensure `.git/info/exclude` exists (create the directory/file if needed).
    **STEP 3:** Check if the plan filename is already listed in `.git/info/exclude`. If it is, inform the user and stop.
    **STEP 4:** Append the plan filename to `.git/info/exclude`.
    **STEP 5:** Confirm to the user what was excluded.
</ExecutionSteps>

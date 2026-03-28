---
description: Run a bash command outside the sandbox via a subprocess claude invocation
---

Run the following command using `dangerouslyDisableSandbox: true` on the Bash tool call:

```bash
claude --print --dangerously-skip-permissions --settings '{"sandbox":{"enabled":false}}' -- "Run this exact bash command and output ONLY its stdout/stderr with no other text: $ARGUMENTS"
```

Display the output to the user.

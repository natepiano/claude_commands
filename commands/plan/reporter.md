---
description: Report progress for an active delegated-plan run once or at a fixed interval
---

# Reporter

`$ARGUMENTS` — optional: `list`, `once`, `watch`, `--run-id <id>`,
`--session-dir <dir>`, or `--interval <seconds>`. A unique run-id prefix is
accepted.

Run outside the sandbox because the script launches the configured Codex or
Claude reporter agent:

```bash
bash ~/.claude/scripts/delegate/reporter.sh $ARGUMENTS
```

No arguments reports once. `watch` reports immediately, repeats at the command
line interval or `config/timings.conf`, and exits when the run ends. Run `watch`
in a dedicated persistent terminal and relay its output without interpretation.
The terminal shows a status spinner while the read-only reporter agent runs;
each result shows reporter-agent time separately from total status time. Do not
inspect the code or produce a second status estimate in the invoking agent.

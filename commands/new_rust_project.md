# New Rust Project

Scaffold a new Rust project from `natepiano/rust-template` and push to GitHub.

**Arguments**: `$ARGUMENTS` — project name and optional flags

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**
**STEP 1:** Execute <ParseArguments/>
**STEP 2:** Execute <RunScript/>
</ExecutionSteps>

<ParseArguments>
If `$ARGUMENTS` is provided and non-empty:
- Parse the project name (first token)
- Parse optional flags: `--lib`, `--no-bevy`
- Proceed directly to <RunScript/>

If `$ARGUMENTS` is empty:
- Ask the user for the project name
- Ask: "Bevy project?" (yes/no — maps to `--no-bevy` if no)
- Ask: "Binary or library?" (bin/lib — maps to `--lib` if lib)
- Wait for all answers before proceeding
</ParseArguments>

<RunScript>
Run the scaffolding script with the resolved arguments. This script uses `gh` so it **must** run unsandboxed.

```bash
~/.claude/scripts/new_rust_project.sh <project-name> [--lib] [--no-bevy]
```

Use `dangerouslyDisableSandbox: true` for this command.

If the script fails, show the error output and stop.
If it succeeds, report the local path and GitHub URL.
</RunScript>

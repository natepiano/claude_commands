# New Rust Project

Scaffold a new Rust project from `natepiano/rust-template`.

**Arguments**: `$ARGUMENTS` — project name and optional flags

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**
**STEP 1:** Execute <ParseArguments/>
**STEP 2:** Execute <RunScript/>
</ExecutionSteps>

<ParseArguments>
Parse `$ARGUMENTS` for a project name (first token) and optional flags: `--lib`, `--no-bevy`.

If no project name was provided, ask the user for one.

Then, for any of the following that were NOT explicitly provided as flags, ask the user:
- "Bevy project?" (yes/no — maps to `--no-bevy` if no)
- "Binary or library?" (bin/lib — maps to `--lib` if lib)

Wait for all answers before proceeding.
</ParseArguments>

<RunScript>
Run the scaffolding script with the resolved arguments.

```bash
~/.claude/scripts/new_rust_project/rust_generate.sh <project-name> [--lib] [--no-bevy]
```

Use `dangerouslyDisableSandbox: true` for this command.

If the script fails, show the error output and stop.
If it succeeds, report the local path. Remind the user they can use `/add_github_repo` to create a GitHub repo for it.
</RunScript>

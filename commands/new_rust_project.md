# New Rust Project

Scaffold a new Rust project from `natepiano/rust-template`.

**Arguments**: `$ARGUMENTS` — project name and optional flags

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**
**STEP 1:** Execute <ParseArguments/>
**STEP 2:** Execute <RunScript/>
</ExecutionSteps>

<ParseArguments>
If `$ARGUMENTS` is provided and non-empty:
- Parse the project name (first token)
- Parse optional flags: `--lib`, `--no-bevy`, `--include-github-repo`
- If `--include-github-repo` is NOT in the arguments, proceed to <AskGitHub/>
- Otherwise proceed directly to <RunScript/>

If `$ARGUMENTS` is empty:
- Ask the user for the project name
- Ask: "Bevy project?" (yes/no — maps to `--no-bevy` if no)
- Ask: "Binary or library?" (bin/lib — maps to `--lib` if lib)
- Wait for all answers before proceeding to <AskGitHub/>
</ParseArguments>

<AskGitHub>
Ask the user: "Create a GitHub repo for this project?" (yes/no — maps to `--include-github-repo` if yes)
Wait for the answer before proceeding.
</AskGitHub>

<RunScript>
Run the scaffolding script with the resolved arguments. If `--include-github-repo` is used, this script uses `gh` so it **must** run unsandboxed.

```bash
~/.claude/scripts/rust_generate.sh <project-name> [--lib] [--no-bevy] [--include-github-repo]
```

Use `dangerouslyDisableSandbox: true` for this command.

If the script fails, show the error output and stop.
If it succeeds, report the local path (and GitHub URL if `--include-github-repo` was used).
</RunScript>

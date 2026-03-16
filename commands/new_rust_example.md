# New Rust Example

Generate a crate example (`examples/<name>.rs`) in the current project.

**Arguments**: `$ARGUMENTS` — example name

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**
**STEP 1:** Execute <ParseArguments/>
**STEP 2:** Execute <RunScript/>
</ExecutionSteps>

<ParseArguments>
If `$ARGUMENTS` is provided and non-empty:
- Parse the example name (first token)
- Proceed directly to <RunScript/>

If `$ARGUMENTS` is empty:
- Ask the user for the example name
- Wait for the answer before proceeding
</ParseArguments>

<RunScript>
Run the scaffolding script with the `--example` flag from the current working directory.

```bash
~/.claude/scripts/new_rust_project/rust_generate.sh <example-name> --example
```

Use `dangerouslyDisableSandbox: true` for this command.

If the script fails, show the error output and stop.
If it succeeds, report the path to the generated example.
</RunScript>

# Add GitHub Repo

Create a GitHub repo for an existing local project and push.

**Arguments**: `$ARGUMENTS` — optional project name (defaults to current directory name)

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**
**STEP 1:** Execute <ResolveProject/>
**STEP 2:** Execute <CreateRepo/>
</ExecutionSteps>

<ResolveProject>
If `$ARGUMENTS` is provided and non-empty:
- Use the first token as the project name
- The project directory is `~/rust/<project-name>`

If `$ARGUMENTS` is empty:
- Use the current working directory
- The project name is the directory basename

Verify the directory exists and has a git repo. If not, show an error and stop.
</ResolveProject>

<CreateRepo>
Run the following from the project directory. This uses `gh` so it **must** run unsandboxed.

```bash
cd <project-dir> && gh repo create "natepiano/<project-name>" --public --source . --push
```

Use `dangerouslyDisableSandbox: true` for this command.

If it fails, show the error and suggest the manual recovery command.
If it succeeds, report the GitHub URL: `https://github.com/natepiano/<project-name>`
</CreateRepo>

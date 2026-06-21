# New Rust Project

Scaffold a new Rust crate from the local `rust-template` checkout (`~/rust/rust-template`, via `cargo generate --path`) — either as a standalone repo under `~/rust/`, or as a member of the Cargo workspace you're currently in. The command infers which from the invocation directory and confirms before proceeding.

**Arguments**: `$ARGUMENTS` — project name and optional flags

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**
**STEP 1:** Execute <ParseArguments/>
**STEP 2:** Execute <DetectMode/>
**STEP 3:** Execute <CollectOptions/>
**STEP 4:** Execute <RunScript/>
</ExecutionSteps>

<ParseArguments>
First token of `$ARGUMENTS` is the project name. If none was provided, ask the user for one.

Recognized flags (any not given are resolved interactively later):
- `--lib` — library crate (default is `bin`)
- `--no-bevy` — skip Bevy
- `--standalone` — force a standalone repo, skipping workspace detection
- `--published` — (member only) per-crate version instead of `version.workspace`
- `--shared-dep` — (member only) register a path dep in `[workspace.dependencies]`
</ParseArguments>

<DetectMode>
If `--standalone` was given, mode is **standalone** — skip detection.

Otherwise infer whether the invocation directory is inside a Cargo workspace:

```bash
root=$(cargo locate-project --workspace --message-format plain 2>/dev/null) \
  && grep -q '^\[workspace\]' "$root" \
  && dirname "$root"
```

- **Prints a path** — a workspace root. This is an inference, not a decision: **confirm** before treating the new crate as a member. Ask:
  `Detected Cargo workspace at <root>. Add <name> as a member under crates/<name>? (Yes / make it a standalone repo in ~/rust/<name>)`
  Wait for the answer. `Yes` → **member** mode with `WORKSPACE_ROOT=<root>`. Otherwise → **standalone**.
- **Prints nothing** (not in a workspace) — mode is **standalone**.
</DetectMode>

<CollectOptions>
For each of these not already fixed by a flag, ask the user:
- "Bevy project?" (yes/no — maps to `--no-bevy` if no)
- "Binary or library?" (bin/lib — maps to `--lib` if lib)

**Member mode only**, also ask (unless already given as flags):
- "Crate description?" → `--description "<text>"`
- **Keywords** (`--keywords "<csv>"`) and **categories** (`--categories "<csv>"`) — both required; `clippy::cargo_common_metadata` (denied via the workspace `cargo` lint group) treats empty values as missing on any publishable member. Before asking, survey what the workspace already uses so you can offer a sensible default rather than a blank prompt:
  ```bash
  # frequency-ranked keywords across existing members
  rg -No 'keywords\s*=\s*\[([^]]*)\]' -r '$1' "$WORKSPACE_ROOT"/crates/*/Cargo.toml \
    | tr ',' '\n' | tr -d ' "' | grep -v '^$' | sort | uniq -c | sort -rn
  # frequency-ranked categories
  rg -No 'categories\s*=\s*\[([^]]*)\]' -r '$1' "$WORKSPACE_ROOT"/crates/*/Cargo.toml \
    | tr ',' '\n' | tr -d ' "' | grep -v '^$' | sort | uniq -c | sort -rn
  ```
  Present the common values (e.g. "Most members use `bevy`, `gamedev`; categories `game-development`, `game-engines`") and ask the user to **accept the suggested set or change it**. Keep refining until they confirm both lists are non-empty.
- "Published independently to crates.io (its own version + crates.io metadata)?" (yes/no → `--published`)
- "Will other workspace crates depend on it (registers a path dep in `[workspace.dependencies]`)?" (yes/no → `--shared-dep`)

Wait for all answers before proceeding.
</CollectOptions>

<RunScript>
Run the scaffolding script with the resolved arguments. Use `dangerouslyDisableSandbox: true`.

**Standalone:**
```bash
~/.claude/scripts/new_rust_project/rust_generate.sh <name> [--lib] [--no-bevy]
```

**Workspace member:**
```bash
~/.claude/scripts/new_rust_project/rust_generate.sh <name> --workspace-root <root> [--lib] [--no-bevy] [--published] [--description "<text>"] --keywords "<csv>" --categories "<csv>" [--shared-dep]
```

If the script fails, show the error output and stop.

On success:
- **Standalone** — report the local path. Remind the user they can use `/add_github_repo` to create a GitHub repo for it.
- **Member** — report the member path. It is already built, formatted, enrolled in nightly clean-fix, and committed to the workspace repo. Tell the user to review and push when ready. Do **not** suggest `/add_github_repo` — a member has no repo of its own.
</RunScript>

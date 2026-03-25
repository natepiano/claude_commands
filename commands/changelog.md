**IMPORTANT** don't commit any changes. Just do the following:

**Arguments**: $ARGUMENTS (optional: issue/PR number, e.g. `#123` or `123`)

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    **STEP 1:** Execute <AnalyzeChanges/>
    **STEP 2:** Execute <ResolveContext/>
    **STEP 3:** Execute <GenerateEntry/>
    **STEP 4:** Execute <UserEntryConfirmation/>
    **STEP 5:** Execute <FinalDecision/>
</ExecutionSteps>

<AnalyzeChanges>
Run `bash ~/.claude/scripts/changelog/analyze_changes.sh` to gather git status, diffs, and remote URL in a single command.
If the script reports no uncommitted changes, inform the user and stop.
</AnalyzeChanges>

<ResolveContext>
If $ARGUMENTS is provided:
- Parse the issue/PR number (strip leading `#` if present)
- Use `gh issue view <number>` or `gh pr view <number>` to get the title, author, and URL
- If it's a PR from an external contributor (not the repo owner), note their GitHub username and profile URL

If $ARGUMENTS is not provided:
- No issue/PR context — the entry will be a plain one-liner with no links
</ResolveContext>

<GenerateEntry>
Using the analyzed changes and any resolved context, generate a changelog entry.

**Format rules (Keep a Changelog / keepachangelog.com):**
- Categorize under one of: `Added`, `Changed`, `Fixed`, `Removed`
- The entry MUST be a single bullet point — one line
- Be concise and descriptive: focus on what the user-facing change is
- If an issue/PR number was resolved, append a markdown link: `([#123](https://github.com/owner/repo/issues/123))`
- If a contributing user was identified (external PR author), append: `by [@username](https://github.com/username)`
- Do not editorialize — just state the fact of the change

**Example entries:**
- `- Add BRP MCP server integration for live Bevy entity inspection ([#42](https://github.com/owner/repo/pull/42)) by [@contributor](https://github.com/contributor)`
- `- Fix cargo fmt allowlist not including bevy_panorbit_camera ([#15](https://github.com/owner/repo/issues/15))`
- `- Change hook scripts to use post-tool-use pattern`

Present the proposed entry to the user:

```
**Proposed changelog entry:**

### [Category]
[entry line]
```
</GenerateEntry>

<UserEntryConfirmation>
Present to user:

## Available Actions
- **use** - Use the proposed changelog entry
- **change** - Provide a different entry

Wait for user response.

If user selects **change**: Ask for their revised entry and use it.
If user selects **use**: Use the proposed entry.
</UserEntryConfirmation>

<FinalDecision>
Present to user:

## Available Actions
- **append** - Prepend the entry to CHANGELOG.md under the `[Unreleased]` section
- **abandon** - Stop without modifying any files

Wait for user response.

If user selects **append**:
- If CHANGELOG.md exists in the repo root, find the `[Unreleased]` section
  - If the matching category heading (e.g. `### Added`) already exists under `[Unreleased]`, append the entry there
  - If not, create the category heading under `[Unreleased]` and add the entry
- If CHANGELOG.md does not exist, create it with the Keep a Changelog header and the entry under `[Unreleased]`
- Execute <AppendOutput/>

If user selects **abandon**: Stop without changes.
</FinalDecision>

<AppendOutput>
Format output as:

```
**Changelog updated**

**File**: `CHANGELOG.md`
**Section**: [Unreleased] > [Category]
**Entry**: [the one-liner]
```

**Formatting requirements**:
- Each field on its own line
- File path in code backticks
</AppendOutput>

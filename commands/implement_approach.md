# Implement Approach

Work uninterrupted on a section of the plan until it is complete.

**Arguments**: $ARGUMENTS (plan file path, optionally with a section hint after a colon, e.g. `plan.md:Phase 2`)

<Persona>
@~/.claude/shared/personas/principal_engineer_persona.md
</Persona>

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 0:** Execute <Persona/> to adopt the Principal Engineer persona
    **STEP 1:** Execute <LoadPlanAndIdentifySection/>
    **STEP 2:** Execute <AnalyzeProjectStructure/>
    **STEP 3:** Execute <WorkspaceQuestionnaire/>
    **STEP 4:** Execute <BranchStrategy/>
    **STEP 5:** Execute <ImplementUninterrupted/>
</ExecutionSteps>

---

<LoadPlanAndIdentifySection>
**Goal:** Locate the plan and determine which section to implement

**Steps:**
1. If $ARGUMENTS provided:
   - If contains `:` — split into PLAN_PATH (before colon) and SECTION_HINT (after colon)
   - Otherwise — set PLAN_PATH = $ARGUMENTS, SECTION_HINT = empty
   - Read the plan document
2. If no $ARGUMENTS:
   - Check conversation context for a plan being discussed
   - If found, use that plan
   - If not found: ask the user for the plan path and **STOP**
3. If SECTION_HINT is empty:
   - First, check conversation context — if the discussion clearly points to a specific section (e.g., "ready for Phase 1", just created a branch for it, or the user said "let's start Phase 2"), use that as SECTION_HINT without asking
   - Only if the section is genuinely ambiguous: display the plan's sections/phases as a numbered list, ask "Which section should I implement?", and **STOP**
4. Store PLAN_PATH, SECTION_CONTENT, and SECTION_NAME for subsequent steps
</LoadPlanAndIdentifySection>

---

<AnalyzeProjectStructure>
**Goal:** Quick assessment of where work can be applied — do NOT launch a deep analysis

**Principle:** You almost certainly already have enough context from the conversation, the plan, and any prior work in this session. Use what you know. If you're unsure, ask the user rather than scanning the codebase.

**Steps:**
1. From what you already know (conversation context, plan content, prior file reads):
   - Determine PROJECT_TYPE: "lib" | "bin" | "both"
   - Recall any examples you've seen or that the plan references
   - Identify which example (if any) relates to the section being implemented
2. Only if you genuinely have no idea about the project layout:
   - Glance at `Cargo.toml` and `examples/` — but do not read file contents or analyze imports
3. Store:
   - PROJECT_TYPE
   - EXISTING_EXAMPLES = [examples you're aware of]
   - SUGGESTED_EXAMPLE = your best guess, or empty

**Do NOT:** Read example source files, scan imports, grep for feature usage, or launch subagents. This step should take seconds, not minutes.
</AnalyzeProjectStructure>

---

<WorkspaceQuestionnaire>
**Goal:** Determine where to apply the implementation work

**Principle:** You have already analyzed the project and the plan section. Lead with a specific recommendation and explain why. Do not present a neutral menu — frame the question around what you believe is the best target.

**Steps:**
1. Form a recommendation based on SECTION_CONTENT and project analysis:
   - If an existing example closely relates to the feature being implemented → recommend that example by name and say why it fits
   - If the feature is new and self-contained enough to demonstrate in isolation → recommend creating a focused example
   - If PROJECT_TYPE includes "bin" and the work clearly belongs in the main application → recommend applying directly to the binary

2. Present the questionnaire with your recommendation leading:

```
## Where should I apply this work?

Based on [your reasoning], I'd recommend **[your specific recommendation]**.

**1. new example** — Create a focused example for [feature name]
   [One line: what the example would contain and demonstrate]

**2. existing example** — Apply to `[SUGGESTED_EXAMPLE or "an existing example"]`
   [One line: why this example is a good fit, or list available examples if no clear match]

**3. apply directly** — Implement in [target file or module name]
   [One line: what gets changed in the main codebase]

Select: 1, 2, or 3
```

NOTE: Option 3 is always shown. For a library with no binary, it means applying directly to the library source. For a binary, it means applying to the application itself.

3. **STOP** and wait for user response. Do NOT proceed until a valid selection is received.

4. **Keyword Handling:**
   - "1", "new", "new example", "create" → WORKSPACE = new_example
   - "2", "existing", "use [name]" → WORKSPACE = existing_example (capture name if provided)
   - "3", "apply", "direct", "directly" → WORKSPACE = apply_direct
   - Any other response → Ask for clarification

5. **Follow-up based on selection:**

   **If WORKSPACE = new_example:**
   - Propose a name for the example based on the feature
   - Ask: "I'll create `examples/[proposed_name].rs` — good, or different name?"
   - **STOP** and wait for confirmation
   - Store WORKSPACE_TARGET = confirmed example path

   **If WORKSPACE = existing_example and no specific example was named:**
   - Present numbered list of EXISTING_EXAMPLES
   - Ask user to pick one
   - **STOP** and wait
   - Store WORKSPACE_TARGET = selected example path

   **If WORKSPACE = apply_direct:**
   - Identify the relevant source files from the plan section
   - Store WORKSPACE_TARGET = those file paths
</WorkspaceQuestionnaire>

---

<BranchStrategy>
**Goal:** Confirm the git branch for this work — never silently create branches

**Principle:** The user does not want stray branches accumulating. Never create a branch without asking. Always check whether we're already on an appropriate branch first.

**Steps:**
1. Check the current git branch (`git branch --show-current`) and recent context
2. If we are already on a non-main feature branch:
   - Suggest working on the current branch: "We're on `[branch-name]` — I'll work here. Good?"
   - **STOP** and wait for confirmation
3. If we are on `main` (or the repo's default branch):
   - Ask the user: "We're on `main`. Want me to create a branch for this work? If so, what should it be called?"
   - **STOP** and wait for the user's response
   - If the user provides a name, create the branch and switch to it
   - If the user says to stay on main, proceed on main
4. **NEVER** create a branch without the user's explicit approval and chosen name
</BranchStrategy>

---

<ImplementUninterrupted>
**Goal:** Implement the entire plan section without stopping

**CRITICAL DIRECTIVE**: Work autonomously until the section is fully implemented. Do NOT pause to ask questions, show progress, or request confirmation at intermediate steps. The only reason to stop is a structural deviation from the plan that cannot be resolved without user input.

**CRITICAL DIRECTIVE**: If approaching auto-compact, write `.handoff_notes.md` with:
- WORKSPACE_TARGET and WORKSPACE type
- Current progress — what is done, what remains
- Any partial work or implementation decisions made
Then continue working. After auto-compact, read `.handoff_notes.md` first to resume.

**Steps:**

1. **If WORKSPACE = new_example:**
   - Create the example file with minimal scaffolding needed to exercise the feature
   - Add any required dependencies or feature flags to `Cargo.toml`

2. **Ensure `bevy_brp_extras` is available:**
   - Check whether the workspace target (example or binary) already depends on `bevy_brp_extras`
   - If not, add it as a dependency so the app is BRP-enabled and can be launched/inspected via BRP tools
   - This is not optional — the implementation loop below relies on being able to launch and inspect the app

3. Read the full SECTION_CONTENT from the plan and break it into implementation steps internally

4. **For each step:**
   a. Implement the change
   b. Build to verify (`cargo build`, or `cargo build --example [name]` for examples)
   c. If build fails — fix it immediately, do not stop to report the failure

5. **Launch and verify at runtime when useful:**
   - For examples: use `mcp__brp__brp_launch_bevy_example` with `target_name` set to the example name
   - For binaries: use `mcp__brp__brp_launch_bevy_app` with `target_name` set to the binary name
   - **Setting log levels:** Use the `env` parameter on the launch tool call to set `RUST_LOG`, e.g. `{"env": {"RUST_LOG": "info,my_crate=debug"}}`. Do NOT set `RUST_LOG` in the shell or in code — always pass it through the launch tool's `env` parameter.
   - **Reading logs:** Use `mcp__brp__brp_read_log` (with optional `keyword` filter and `tail_lines`) or read the log file directly — whichever is faster for what you need
   - **Shutting down:** Use `mcp__brp__brp_shutdown` before relaunching after code changes
   - You do not need to launch after every change — use your judgement. Launch when you need to verify runtime behavior, not just compilation.

6. **After all steps are complete:**
   - Run `cargo clippy --workspace --all-targets --all-features -- -D warnings` and fix issues
   - Run `cargo +nightly fmt`
   - Run `cargo nextest run` if the section includes testable behavior

5. **Present completion summary:**

```
## Section Complete: [SECTION_NAME]

**Workspace:** [WORKSPACE_TARGET]

**What was implemented:**
- [List of changes]

**Build:** [pass/fail]
**Clippy:** [clean/issues fixed]
**Tests:** [pass/fail/not applicable]
```

6. Clean up `.handoff_notes.md` if it exists
</ImplementUninterrupted>

<Persona>
@~/.claude/shared/personas/principal_engineer_persona.md

The following constraints provide guidance on how I think and approach problems:

@~/.claude/shared/constraints/code_review_constraints.md
</Persona>

<LoadStyleGuide>
Run: `cat ~/rust/rust_style/style/*.md`
Internalize all rules. You will compare each clippy/mend fix against this set.
</LoadStyleGuide>

<AnalyzeFixes>
Review the clippy and mend issues that were just fixed in this conversation.
For each issue, classify it into one of three categories:

1. **Already covered** — A matching rule exists in the style guide. You didn't follow it.
2. **Covered but needs improvement** — A rule exists but its examples or wording don't clearly address this case. Propose an update.
3. **Missing** — No rule covers this pattern. Propose a new rule.

Present the assessment as a table:

| # | Issue | Category | Style rule (if exists) | Action |
|---|-------|----------|----------------------|--------|

Then for each issue in category 2 or 3, present the proposed rule or edit.
</AnalyzeFixes>

<UserDecision>
## Available Actions
- **apply** - Write all proposed new rules and updates
- **change** - Modify specific proposals before writing
- **skip** - Don't update the style guide

Wait for user response.
</UserDecision>

<WriteRules>
For new rules: write to `~/rust/rust_style/style/[kebab-case-name].md` with standard frontmatter.
For updates: edit the existing file in place.
</WriteRules>

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 0:** Execute <Persona/>
**STEP 1:** Execute <LoadStyleGuide/>
**STEP 2:** Execute <AnalyzeFixes/> — assess each fix against the style guide
**STEP 3:** Execute <UserDecision/>
**STEP 4:** If **apply** or **change**: Execute <WriteRules/>
</ExecutionSteps>

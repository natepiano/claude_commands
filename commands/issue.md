Create a new issue in the hanadocs vault.

The hanadocs vault lives at an absolute path on disk so this command works from any project. Do NOT use paths relative to the current working directory.

- Vault root: `/Users/natemccoy/rust/hanadocs`
- Template: `/Users/natemccoy/rust/hanadocs/.claude/templates/issue.yaml`
- Issues directory: `/Users/natemccoy/rust/hanadocs/issues/`
- Project base files: `/Users/natemccoy/rust/hanadocs/issues - <short>.base`

## Arguments

$ARGUMENTS contains: `<project-shorthand> <issue title on first line>\n<optional body content on remaining lines>`

## Project mapping

Look up the project shorthand from the base files in the vault root (`/Users/natemccoy/rust/hanadocs/issues - <short>.base`). The mapping is:

| Shorthand | Project link | Base file |
|-----------|-------------|-----------|
| bei | bevy_enhanced_input | issues - bei.base |
| biz | biz | issues - biz.base |
| brp | bevy_brp_mcp | issues - brp.base |
| bwm | bevy_window_manager | issues - bwm.base |
| cargo-mend | cargo-mend | issues - cargo-mend.base |
| cargo-port | cargo-port | issues - cargo-port.base |
| catenary | bevy_catenary | issues - catenary.base |
| diegetic | bevy_diegetic | issues - diegetic.base |
| fairy_dust | fairy_dust | issues - fairy_dust.base |
| hana | hana | issues - hana.base |
| lagrange | bevy_lagrange | issues - lagrange.base |
| liminal | bevy_liminal | issues - liminal.base |
| nateroids | nateroids | issues - nateroids.base |
| panorbit | bevy_panorbit_camera | issues - panorbit.base |
| rust-template | rust-template | issues - rust-template.base |

If the shorthand doesn't match any of these, tell the user and stop.

## Process

1. Parse $ARGUMENTS: the first word is the project shorthand. The rest of the first line is the issue title. Any subsequent lines are body content.

2. Derive the filename from the title: lowercase, keep spaces as-is (do NOT replace spaces with hyphens), remove special characters (including any literal hyphens — replace hyphens with spaces), collapse runs of whitespace, append `.md`. The file goes in `/Users/natemccoy/rust/hanadocs/issues/`. Example: title `fix branch name in ci run labels` → `fix branch name in ci run labels.md`.

3. **Title length check**: If the derived filename (without `.md`) exceeds 50 characters, the title is too long for a filename. Ask the user to pick a shorter filename by offering 3 concise alternatives (3-6 words each) as options. The original long title then becomes a `##` heading at the top of the issue body.

4. Before creating the file, collect metadata from the user. Combine the area/category prompt with step 3 into a single prompt if the title is too long.

   **Area and category:**
   - **Area**: What area is this issue in? (e.g. cursor, networking, ui, rendering, animation, testing, tooling — or press enter for "unfiled")
   - **Category**: What category? Options: bug, feature, enhancement, docs, example, refactor, idea, research, business (or press enter for "unfiled")

   **Prioritization survey (required — every new issue is fully rated at creation).** Ask via `AskUserQuestion`. The four-question-per-call cap means two calls: first `backlog_goal` + `backlog_alignment` + `backlog_impact` + `backlog_urgency`, then `backlog_effort`. For each factor present all five levels as options — the option label is the star string, the description is the definition below. Store `backlog_goal` as a scalar (e.g. `1 - Ship Hana`) and each rating as a double-quoted YAML text scalar of one-to-five `⭐` characters. Do NOT collect or write `backlog_score` / `backlog_rank`; the vault's background watcher computes those from these inputs.

   - **`backlog_goal`** — the one goal this issue most directly advances (pick exactly one):
     - `1 - Ship Hana` — make Hana a coherent, usable, releasable product
     - `2 - Find Collaborators` — make the work inviting and practical for contributors and partners
     - `3 - Seek Investors` — build and communicate a credible investment case
     - `4 - Business Viability` — legal, operational, and commercial sustainability

   - **`backlog_alignment`** — how strongly it advances the selected goal:
     - `⭐` — weak or minimal relationship to the goal
     - `⭐⭐` — indirectly supports the goal or removes a limited obstacle
     - `⭐⭐⭐` — directly advances a meaningful part of the goal
     - `⭐⭐⭐⭐` — central work on which substantial goal progress depends
     - `⭐⭐⭐⭐⭐` — completing the issue itself delivers a major goal outcome

   - **`backlog_impact`** — magnitude of benefit if completed (exclude urgency and effort):
     - `⭐` — small or highly localized benefit
     - `⭐⭐` — clear but limited benefit to a narrow workflow or audience
     - `⭐⭐⭐` — significant benefit to an important workflow or audience
     - `⭐⭐⭐⭐` — major benefit across a core workflow or multiple audiences
     - `⭐⭐⭐⭐⭐` — transformative outcome for the product, organization, or ecosystem

   - **`backlog_urgency`** — cost of delay (no duration estimate; four- and five-star require concrete time-pressure evidence):
     - `⭐` — can wait; delay has little material cost
     - `⭐⭐` — pressure is building; delay slowly raises cost or loses opportunity
     - `⭐⭐⭐` — pressing; delay materially worsens the outcome or problem
     - `⭐⭐⭐⭐` — time-sensitive; an active commitment, dependency, or opportunity is at risk
     - `⭐⭐⭐⭐⭐` — immediate; serious current harm, blockage, or a hard cutoff

   - **`backlog_effort`** — relative breadth and coordination, never elapsed time (more stars mean more work, not a better issue):
     - `⭐` — XS: one atomic, tightly scoped action
     - `⭐⭐` — S: contained work with few touchpoints and a known approach
     - `⭐⭐⭐` — M: several coordinated steps or touchpoints
     - `⭐⭐⭐⭐` — L: broad work requiring substantial coordination or integration
     - `⭐⭐⭐⭐⭐` — XL: a multi-phase initiative or epic, likely a decomposition candidate

5. Read the template at `/Users/natemccoy/rust/hanadocs/.claude/templates/issue.yaml` and fill in:
   - `{{PROJECT}}` — the full project link name
   - `{{SHORT}}` — the project shorthand
   - `{{DATE}}` — today's date in YYYY-MM-DD format
   - `{{AREA}}` — the area the user provided (or "unfiled" if skipped)
   - `{{CATEGORY}}` — the category the user provided (or "unfiled" if skipped)
   - `{{BACKLOG_GOAL}}` — the selected goal string, e.g. `1 - Ship Hana`
   - `{{BACKLOG_ALIGNMENT}}` — the chosen alignment stars (one to five `⭐`)
   - `{{BACKLOG_IMPACT}}` — the chosen impact stars
   - `{{BACKLOG_URGENCY}}` — the chosen urgency stars
   - `{{BACKLOG_EFFORT}}` — the chosen effort stars

6. If the category is "unfiled", set the category frontmatter to:
   ```yaml
   category:
     - "[[issue structure#unfiled|unfiled]]"
   ```

7. If the area is "unfiled", set the area frontmatter to:
   ```yaml
   area: "[[unfiled]]"
   ```

8. Create the file at `/Users/natemccoy/rust/hanadocs/issues/<derived-filename>.md` with the filled-in frontmatter followed by the body content. If the title was shortened in step 3, prepend the original long title as a `## ` heading before the body content.

9. Show the user the created file path and a summary of what was created.

10. Report the ranking. The vault's background watcher computes `backlog_score` and `backlog_rank` from the survey inputs about 500ms after the file is written. Wait about a minute for it to settle, then re-read the created file's frontmatter and report `backlog_rank` (and `backlog_score`) to the user, so they see where the new issue landed in the global ordering.

## Important
- Do NOT commit the changes
- The file must go in `/Users/natemccoy/rust/hanadocs/issues/` (absolute path)
- Today's date comes from the MEMORY.md `# currentDate` section, or use the system date
- This command works from any project's working directory — never resolve paths relative to the CWD

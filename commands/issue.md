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

4. Before creating the file, ask the user two questions (combine with step 3 into a single prompt if the title is too long):
   - **Area**: What area is this issue in? (e.g. cursor, networking, ui, rendering, animation, testing, tooling — or press enter for "unfiled")
   - **Category**: What category? Options: bug, feature, enhancement, docs, example, refactor, idea, research, business (or press enter for "unfiled")

5. Read the template at `/Users/natemccoy/rust/hanadocs/.claude/templates/issue.yaml` and fill in:
   - `{{PROJECT}}` — the full project link name
   - `{{SHORT}}` — the project shorthand
   - `{{DATE}}` — today's date in YYYY-MM-DD format
   - `{{AREA}}` — the area the user provided (or "unfiled" if skipped)
   - `{{CATEGORY}}` — the category the user provided (or "unfiled" if skipped)

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

## Important
- Do NOT commit the changes
- The file must go in `/Users/natemccoy/rust/hanadocs/issues/` (absolute path)
- Today's date comes from the MEMORY.md `# currentDate` section, or use the system date
- This command works from any project's working directory — never resolve paths relative to the CWD

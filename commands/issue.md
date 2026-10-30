---
description: Create a new issue in the hanadocs vault from a project shorthand, title, and optional body.
---

Create a new issue in the hanadocs vault.

The hanadocs vault lives at an absolute path on disk so this command works from any project. Do NOT use paths relative to the current working directory.

- Vault root: `~/rust/hanadocs`
- Template: `~/rust/hanadocs/.claude/templates/issue.yaml`
- Issues directory: `~/rust/hanadocs/issues/`
- Project base files: `~/rust/hanadocs/home-base/issues - <short>.base`

## Arguments

$ARGUMENTS contains: `<project-shorthand> <issue title on first line>\n<optional body content on remaining lines>`

## Project mapping

Read the canonical project from `~/rust/hanadocs/home-base/issues - <short>.base` (`vault.load_project_names()` in `scripts/issue_score/vault.py`). These base files are the only shorthand source. If the base file is missing, tell the user and stop.

## Process

1. Parse $ARGUMENTS: the first word is the project shorthand. The rest of the first line is the issue title. Any subsequent lines are body content.

2. Derive the filename from the title: lowercase, keep spaces as-is (do NOT replace spaces with hyphens), remove special characters (including any literal hyphens — replace hyphens with spaces), collapse runs of whitespace, append `.md`. The file goes in `~/rust/hanadocs/issues/`. Example: title `fix branch name in ci run labels` → `fix branch name in ci run labels.md`.

3. **Title length check**: If the derived filename (without `.md`) exceeds 50 characters, the title is too long for a filename. Ask the user to pick a shorter filename by offering 3 concise alternatives (3-6 words each) as options. The original long title then becomes a `##` heading at the top of the issue body.

4. Before creating the file, propose the metadata in one reply, combined with step 3's filename choice when the title is too long. The user confirms or changes it in their reply; write the file only after that. Never ask field by field, and never through a form.

   Run `with-typesafe ~/.claude/scripts/lib/py ~/.claude/scripts/issue_score/score_issues.py propose --project <short> --title <title>`; when there is body content, save it to a temporary UTF-8 file and add `--body-file <path>`. Quote each argument. The first output line has one top value per field; the separate `Alternatives:` line holds unsure choices. Use the returned values for the fields in `~/.claude/scripts/issue_score/questions.py`'s `JEV_FIELDS`; propose the fields marked `(agent proposes)` using the rubric below. Keep the flags and show the unsure Jev alternatives so the user picks in the same reply. When goal is unsure, keep each candidate's alignment and use the alignment belonging to the goal the user picks. On exit 2, say the call failed and propose every field using the rubric below.

   - **Area**: one area from the `area` column of `~/rust/hanadocs/conf/issue-areas.md`, or "unfiled"; when `area` is in `JEV_FIELDS`, use the `propose` value as the proposal.
   - **Category**: one of bug, feature, enhancement, docs, example, refactor, idea, research, business, or "unfiled".
   - **Ranking (required — every new issue is fully rated at creation).** All five values in one plain-text line, each with the value you propose from the definitions below (urgency defaults to one star), for example `goal 1 - Ship Hana · alignment ⭐⭐⭐ · impact ⭐⭐⭐ · urgency ⭐ · effort ⭐⭐`. Store `backlog_goal` as a scalar (e.g. `1 - Ship Hana`) and each rating as a double-quoted YAML text scalar of one-to-five `⭐` characters. Do NOT collect or write `backlog_score` / `backlog_rank`; the vault's background watcher computes those from these inputs.
   - **Dependencies (optional).** The existing open issues, if any, you propose must rank ahead of this issue. Record each as a wikilink in `depends_on`; use `[]` when there are none. Before creating the issue, resolve every supplied wikilink to an issue file. Closed targets are satisfied and are ignored by ranking. An unresolved link, duplicate target, self-reference, or open dependency missing prioritization metadata is invalid; a dependency cycle prevents ranking.

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

   - **`backlog_urgency`** — cost of delay, rated against the calendar rather than against other issues (no duration estimate). Four and five stars require concrete time-pressure evidence — a committed date, an external cutoff, an expiring opportunity, or active harm — and carry heavy scoring weight; five stars sends the issue straight to the top of the backlog. Wanting to work on something next is alignment or impact, not urgency. One star is the default and is correct for most issues:
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

   A proposal filed from a session with no user in it, such as a unit's, leaves the five ranking fields empty and puts the single-valued first line on a `Proposed ranking:` line at the top of the body, with unsure alternatives on a separate `Alternatives:` line. Never put alternative values on `Proposed ranking:`. The fields are written when the user confirms them on review (`/showrunner:proposal`).

5. Read the template at `~/rust/hanadocs/.claude/templates/issue.yaml` and fill in:
   - `{{PROJECT}}` — the full project link name
   - `{{SHORT}}` — the project shorthand
   - `{{DATE}}` — today's date in YYYY-MM-DD format
   - `{{AREA}}` — the confirmed area (or "unfiled")
   - `{{CATEGORY}}` — the confirmed category (or "unfiled")
   - `{{BACKLOG_GOAL}}` — the confirmed goal string, e.g. `1 - Ship Hana`
   - `{{BACKLOG_ALIGNMENT}}` — the confirmed alignment stars (one to five `⭐`)
   - `{{BACKLOG_IMPACT}}` — the confirmed impact stars
   - `{{BACKLOG_URGENCY}}` — the confirmed urgency stars
   - `{{BACKLOG_EFFORT}}` — the confirmed effort stars
   - `{{DEPENDS_ON}}` — a YAML inline list of existing issue wikilinks, e.g. `["[[render backend]]"]`, or `[]`
   - `{{PROPOSED_BY}}` — for a proposal, the unit or session that wrote it; empty otherwise
   - `{{SHOWRUNNER}}`, `{{UNIT}}`, `{{PLAN}}`, `{{AS_BUILT}}` — empty unless already known; `proposal_issue.py` sets them as the work is approved and built (`/showrunner:proposal`)

   A proposal's `tags` are `[issue, proposal]`.

6. If the category is "unfiled", set the category frontmatter to:
   ```yaml
   category:
     - "[[issue structure#unfiled|unfiled]]"
   ```

7. If the area is "unfiled", set the area frontmatter to:
   ```yaml
   area: "[[unfiled]]"
   ```

8. Create the file at `~/rust/hanadocs/issues/<derived-filename>.md` with the filled-in frontmatter followed by the body content. If the title was shortened in step 3, prepend the original long title as a `## ` heading before the body content.

   For a proposal, then give it its P number: `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/proposal_issue.py" number "<issue path>"`.

9. Show the user the created file path and a summary of what was created.

10. Report the ranking. The vault's background watcher computes `backlog_score` and `backlog_rank` from the survey inputs — it does NOT finish within a tool-call round trip. Even a full re-rank measures about 3-4 seconds end to end (0.5s poll detection, debounce, snapshot, apply, validate, settle). Repeatedly re-reading the file in a tight loop just returns nothing several times and invites a false "the watcher isn't running" report.

    Wait for the fields with a single **backgrounded** blocking command, then yield the turn and read the result from the task notification:

    ```bash
    f="$HOME/rust/hanadocs/issues/<derived-filename>.md"
    until grep -q "backlog_rank" "$f"; do sleep 2; done
    grep -E "backlog_score|backlog_rank" "$f"
    cat /tmp/hanadocs-prioritize/last-status
    ```

    Run it with `run_in_background: true` and a `timeout` of 60000. Report `backlog_rank` (and `backlog_score`) to the user, so they see where the new issue landed in the global ordering.

    **Concurrent edits abort the apply.** If the user (or Obsidian) saves any issue file while the apply is in flight, `renumber.py` raises `file changed after discovery`, rolls the whole batch back, and `last-status` goes to `pending ... result=rerun detail=concurrent-change-during-apply`. That is self-healing — the daemon coalesces and retries within a few seconds, and the wait loop above rides through it. Only report a problem if `last-status` is still not `ok ... result=updated` after the timeout; then check `/tmp/hanadocs-prioritize/events.log` for the failure and whether `run_watcher.sh --daemon` is alive.

    If a re-rank ever takes tens of seconds again, check `ProcessType` in `~/.claude/scripts/prioritize/com.natemccoy.hanadocs-prioritize.plist`. `Background` puts the daemon in the DARWIN_BG band, which adds a per-write I/O throttle delay — that alone turned a 0.5s 338-file apply into 38-47s. It must stay `Standard`.

## Important
- Every issue's `return` carries **two** entries: the project base `[[issues - <short>.base]]` first, then `[[issues.base]]`. The template already does this — do not drop either one.
- Do NOT commit the changes
- The file must go in `~/rust/hanadocs/issues/` (absolute path)
- Today's date comes from the MEMORY.md `# currentDate` section, or use the system date
- This command works from any project's working directory — never resolve paths relative to the CWD

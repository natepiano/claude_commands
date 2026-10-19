---
description: File a finished proposal as a hanadocs issue, record the user's approval, and see it started and closed.
---

# Proposal

A proposal is a hanadocs issue tagged `proposal`, in the folder the production
doc's `Proposals` line names. `/showrunner:proposals` lists the open ones. There
is no index to add a row to. Every frontmatter change after filing goes through:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/proposal_issue.py" <verb> <issue> …
```

It edits that one file, sets `date_modified`, and prints what changed. On a
missing file or one without frontmatter it exits 2: report the line.

| State | `status` / `stage` | Set |
| --- | --- | --- |
| written | open / backlog | `proposed_by` |
| approved, not started | open / backlog | `showrunner`, `unit`, `plan` |
| being built | open / active | |
| built | closed | `as_built` |

## File

A finished proposal becomes an issue through `/issue`: tag `proposal` beside
`issue`, and `proposed_by` the unit or session that wrote it. Drafts stay out.
The writer proposes all five ranking values (`backlog_goal`,
`backlog_alignment`, `backlog_impact`, `backlog_urgency`, `backlog_effort`) at
once. The values reach the frontmatter only after the user confirms them, since
the vault ranks every open issue by them. A unit has no user in its session, so
it leaves the five fields empty, puts its values on a `Proposed ranking:` line
at the top of the issue body, and sends the showrunner the issue path, one line,
and the five values.

Do not show a new proposal to the user unasked (user, 2026-10-09: proposals
accumulate for them to review). When they review one, show its proposed values
in one plain-text line; they confirm or change them in their reply. Then write
all five and delete the `Proposed ranking:` line:

```sh
… proposal_issue.py stars <issue> --goal "1 - Ship Hana" --alignment 3 --impact 3 --urgency 1 --effort 2
```

Never ask star by star, and never through a form.

## Approve

On the user's approval, send the issue path to the unit that builds it. The
unit writes the work into its plan as phases that cite the issue, puts the issue
on the plan's `> **Issues:**` header line (`~/.claude/docs/delegate_plan_format.md`),
and replies with the plan path and phases. Then:

```sh
… proposal_issue.py approve <issue> --showrunner "<name>" --unit <unit> --plan "<plan path> Phase N–M"
```

- `--showrunner`: this session's name as `showrunners.py name <slug>` prints it
  (`hana`, `natedev`), never the production slug.
- `--unit`: the unit's full name in the production doc's **Units** table
  (`trunk-unit`).

The stage stays backlog until the work starts. A proposal the user declines
closes through `/close_issue` with its reason, except an issue tagged `style`:
decline it only through the command below, never `/close_issue`.

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_proposals.py decline --repo <style checkout> --issue <issue> --words '<the user’s words>'
```

## Start and close

The unit's run does both:

- **start**: `/unit:direct` <PrepareSession/> marks each issue on the plan's
  Issues line active.
- **close**: the end-of-run as-built pass
  (`~/.claude/docs/delegate/final_gate_commit.md` → <RunAsBuilt/>) sets
  `as_built` to the doc it wrote and closes each issue.

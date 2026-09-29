---
description: Judge Hana screenshots against the UX guide and report each defect with its rule and fix. Reads only.
---

# UX eval

**Purpose:** judge screenshots of Hana against the UX guide in `~/rust/hanadocs/ux/`
and report every defect with its fix. The bar is the user's: Hana looks as
professional and "slick" as it possibly can be.

**Usage:** `/ux_eval <shot paths or a directory> [--guide <dir>] [--tags <t1,t2>] [--context "<what changed and which states the shots should show>"]`

Run it in a fresh helper context: the showrunner of a production spawns one per
checkpoint, so the shots and the guide stay out of long-lived sessions.

This command reads only. It edits no file; it writes only shrunk copies of the
shots to the session scratchpad.

---

<ExecutionSteps>
**EXECUTE IN ORDER:**

**STEP 1:** <LoadChecklist/>
**STEP 2:** <ShrinkShots/>
**STEP 3:** <Judge/>
**STEP 4:** <Confirm/>
**STEP 5:** <Report/>
</ExecutionSteps>

---

<LoadChecklist>
```sh
zsh ~/.claude/scripts/ux_style/load-ux-style.sh --checklist [--guide <dir>] [--tags <t1,t2>]
```

Pass `--guide` and `--tags` through only when given. Each line reads
`<stem> — <title> — <test>`.
</LoadChecklist>

---

<ShrinkShots>
A directory means every `.png`, `.jpg`, `.jpeg` and `.webp` in it. Shrink each
shot to at most 1200 px on its long side:

```sh
magick <in> -resize '1200x1200>' <scratchpad>/ux_eval/<name>.png
```

A shot that is missing or will not convert gets a row with defect `unreadable`.
</ShrinkShots>

---

<Judge>
View every shrunk shot. Ask each checklist test of each shot.

With `--context`, check that the shots show every state it names. A state no
shot shows is a defect: Shot `—`, Rule `context`, Defect `no shot shows <state>`.

Report any polish defect no rule covers yet as Rule `no rule: <proposed-stem>`.
</Judge>

---

<Confirm>
Before reporting a defect under a rule, load that rule's full text:

```sh
zsh ~/.claude/scripts/ux_style/load-ux-style.sh --rule <stem> [--rule <stem>]... [--guide <dir>]
```

Keep the defect only when the rule's text applies to what the shot shows.
</Confirm>

---

<Report>
```markdown
Verdict: pass | defects

| Shot | Rule | Defect | Fix |
| --- | --- | --- | --- |
| <file name> | <stem> | <what is wrong, and where in the shot> | <the visible change that fixes it> |
```

`pass` means no rows. Then stop.
</Report>

---

## Rules

- Read only. No edits, commits, branches or worktrees.
- Judge only what the shots show. A defect names its place in the shot.
- One defect seen in several shots is one row listing each shot.
- Output nothing but the verdict and the table.

---
description: Judge Hana screenshots against the UX guide and report each defect with its rule and fix. Reads only.
---

# UX eval

**Purpose:** judge screenshots of Hana against the UX guide in `~/rust/hanadocs/ux/`
and report every defect with its fix. The bar is the user's: Hana looks as
professional and "slick" as it possibly can be.

**Usage:** `/ux_eval <shot paths or a directory> [--guide <dir>] [--tags <t1,t2>] [--scale <shot pixels per logical pixel>] [--context "<what changed, which states the shots should show, and each shot's window size>"]`

`--scale` defaults to 1; pass 2 for shots from a Retina display.

Run it in a fresh helper context: the showrunner of a production spawns one per
checkpoint, so the shots and the guide stay out of long-lived sessions.

This command reads only. It edits no file; it writes only shrunk copies and
crops of the shots to the session scratchpad.

---

<ExecutionSteps>
**EXECUTE IN ORDER:**

**STEP 1:** <LoadChecklist/>
**STEP 2:** <ShrinkShots/>
**STEP 3:** <Judge/>
**STEP 4:** <Measure/>
**STEP 5:** <Confirm/>
**STEP 6:** <Report/>
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

<Measure>
Contrast and text size are measured, never judged by eye (`readable-contrast`,
`readable-size`). The number decides: a measured pass is not a defect however
it looks, and a measured fail is a defect however it looks.

In each shot that shows text, measure at least:
- the smallest text, where it looks smallest;
- the text that looks lowest in contrast, where its backdrop looks worst;
- anything else that looks close to either floor.

To place a box, crop the region from the **full-size** shot and view the crop:

```sh
magick <shot> -crop WxH+X+Y +repage <scratchpad>/ux_eval/crop_<name>.png
```

Then box one capital letter or digit (or a short piece of an edge), tight,
with a little background around it, in full-size shot pixels:

```sh
python3 ~/.claude/scripts/ux_style/measure_text.py <shot> --box X,Y,W,H --scale <scale>
```

Size floors hold in a 1440×900 logical window. For in-world text in a shot at
another size, multiply the cap height by 900 ÷ the window's logical height
(from `--context`, or the shot's pixel height ÷ scale). Screen panels are not
scaled. A defect under either rule states its measured number.
</Measure>

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

Under `readable-contrast` and `readable-size`, the Defect names the measured
number and the floor: `Log body 3.3:1 over the lit horizon (floor 4.5:1)`.

`pass` means no rows. Then stop.
</Report>

---

## Rules

- Read only. No edits, commits, branches or worktrees.
- Judge only what the shots show. A defect names its place in the shot.
- One defect seen in several shots is one row listing each shot.
- Output nothing but the verdict and the table.

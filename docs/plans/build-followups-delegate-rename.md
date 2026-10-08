# delegate-rename

> **Production: build-followups** — unit `delegate-rename-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-08 09:20 PDT

The user's words, 2026-10-08 (PDT), in order:
- "okay - if we want to continue with movie industry terms what would be a better name for the delegate skill - and also how much documentation and scripts would need to change to sweep a new name through?"
- The showrunner's summary of the study, which he asked for: "Recommended name: /unit:direct. Recommended sweep: rename the command and the prose people and agents read, leaving internals alone. That is 68 files, at most 277 lines. Main risk: the old name must stay as a real stub file, since a symlink alias failed before."
- On build-report's one unmerged row in delegate.md, the showrunner said: "If the rename goes first, that one row is moved into the new file by hand when build-report merges." The user: "that's what we should do".
- Asked who should do it, the showrunner proposed a new unit: "/unit:direct, the 68-file sweep, old name kept as a stub." The user: "yes call the new unit "delegate-rename"".

The showrunner's notes from the study (check each against the code; they are not the user's words):
- Rename /unit:delegate to /unit:direct: the command file, and the prose that people and agents read (commands, skills, docs). Internals keep their names: the scripts/delegate folder, state paths, hook file names, variable names.
- commands/unit/delegate.md stays as a real stub file that sends its caller to /unit:direct with the same arguments. A symlink alias failed from 09-28 to 10-01. Running unit directors hold the old command text and old launch prompts, so the old name must keep working.
- scripts/hooks/session-start-delegate-resume.py names delegate.md by path.
- scripts/model_study/turns.py must accept both names, since old transcripts hold the old one.
- add_unit.py's launch prompts (prompt_for) and /showrunner:produce and /showrunner:promote_unit name the command.
- Do not touch settings.json. Branch build-followups-build-report holds one unmerged row for delegate.md; leave it to the showrunner at that merge.
- Other units have work in flight in commands/unit (phase-tables-unit: eta.md, eta_breakdown.md, report.md) and commands/showrunner (enh-showrunner-unit). Keep each edit in those files to the lines that name the command.
- Design rule from the user today: write nothing to a file that a script can find quickly.

# phase-tables

> **Production: build-followups** — unit `phase-tables-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-08 08:20 PDT

two things we often ask for

1. phase eta - to be used in the dailies gantt chart (and elsewhere)
2. an overview of upcoming phases -  as a markdown table showing phase number, brief description, ETA (or predicted start / finish)

i would like to have a mechanism to keep this information succinctly up to date so i could just pull it up in hanadocs and look at the updated markdown table  -

we could keep it in sync with our current showrunner structures - i.e.
showrunners/hana
showrunners/natedev
etc.

and then
showrunners/hana/startup.md
showrunners/hana/widget.md
....
showrunners/natedev/showrunner-fixer.md
etc.

and whenever they wake up to do a /unit:report - as part of the /unit:report they update their own markdown file - using their current session name (and deleting an old one if if it was renamed)

this way i can stop asking for these ad hoc - and we have a natural place for them to be maintained

it can show current phase separately - with metadata and then keep the markdown table showing start end times for all past phases and upcoming phases as well - maybe sorted descending so the oldest phases are at the bottom

scripts can aid in this process

so that agents don't have to re-read/re-write every markdown file and possibly drift on format

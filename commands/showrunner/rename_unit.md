---
description: Rename a unit, its folder and its branch.
argument-hint: "<old> <new>"
---

# Rename a unit

Run the command from the owning showrunner session:

```sh
python3 ~/.claude/scripts/production/rename_unit.py <plan|apply|verify> <slug> <old> <new>
```

The command reads the unit from its staff row.
A rerun finishes a pending rename and repairs the displays.
A same-name request keeps the staff name and repairs stale session, tab, and pane displays.
A completed rerun prints one `unchanged` line, exits 0, and sends no restart notice.

A new-name rename writes the staff name and sets the displays.
It reads the current folder from the staff row and the current branch from Git.
It uses `unit_names.swap_stem` for the folder name and branch.
It replaces the trailing `-<old name>` with `-<new name>` independently in the folder and branch.
The folder keeps its parent folder.
For example, renaming `port` to `x` moves `~/rust/cargo-liner-port` to `~/rust/cargo-liner-x`.
The branch `cargo-liner-port` becomes `cargo-liner-x`.
The branch `init/catalyst-lfo` becomes `init/catalyst-<new>` when `lfo` changes.
A folder or branch without the suffix stays unchanged and prints one `anomaly:` line.
The other location still changes when its suffix matches.
The command never computes a folder from a unit name alone.

The move stops the live unit and its live workers.
It moves the Git worktree and changes the branch while the sessions are stopped.
It writes the staff location and resumes the same current sessions in their panes.
A stopped session stays stopped.
Rerun the same command after an interruption.
A restart failure prints one anomaly with `shutdown.py restart --only <staff-id>`.
The command reports stopped rows with the same restart command.
A rerun starts no stopped row.
Run the reported restart command when you want that row to start.

Use the folder-only command to keep the branch:

```sh
python3 ~/.claude/scripts/production/unit_move.py move-folder --unit <staff id> --to <path>
```

The command refuses an existing destination or a concurrent row operation.
The move limits come from `config/production.conf` and respect the current operation deadline.
A Git timeout prints one `rename_unit: error:` line and exits 1.
The folder-only command reports the timeout as an anomaly.
Neither command prints a traceback.

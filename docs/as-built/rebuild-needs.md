# Rebuild needs

`scripts/rebuild/need.py` records needed system rebuilds for `natedev` and `mac`.
The default folder is `~/.local/state/rebuild/needs`.
`REBUILD_NEEDS_DIR` selects a different folder for tests or callers.
The command reads this variable for each operation.

## Record

Each open need has one JSON file named `<machine>-<nixos_commit>.json`.
The machine and full NixOS commit form the key.
The record has these fields:

| Field | Meaning |
| --- | --- |
| `id` | The machine and commit, joined with `-`. |
| `machine` | `natedev` or `mac`. |
| `nixos_commit` | The full commit that needs a rebuild. |
| `reason` | One sentence that explains the rebuild. |
| `concerns` | The staff ID affected by the rebuild. |
| `requested_by` | The requesting staff ID, or an empty string when omitted. |
| `created_at` | The creation time in ISO format with the UTC offset. |
| `cleared_by` | The exact generation log line that cleared a completed need. |

Open records omit `cleared_by`.
`NeedRecord` describes an open record.
`CompletedNeedRecord` adds the required `cleared_by` field for a completed record.
Completed records remain in `needs/done/`.
Record writes use temporary files and atomic file operations.
Adding the same key changes neither an open record nor a completed record.
The needs folder contains one `.lock` file.
`add_need` holds an exclusive `fcntl.flock` from the existence check through the record write.
`clear_needs` holds the same lock while it rewrites and moves each completed record.
The list command takes no lock and stays read-only.

## Commands

Run commands from the repository folder.

```sh
scripts/lib/py -m scripts.rebuild.need add --machine natedev --commit <full-nixos-hash> --reason "Apply the rebuild settings." --concerns <staff-id> --requested-by <staff-id>
scripts/lib/py -m scripts.rebuild.need list
scripts/lib/py -m scripts.rebuild.need list --json
scripts/lib/py -m scripts.rebuild.need clear --machine natedev --generations "$HOME/.local/state/rebuild/generations.log" --nixos-repo /etc/nixos
```

`--requested-by` is optional.
The list command returns one row per machine with open needs.
Each row uses the newest need by creation time.
The row joins all open reasons for that machine with spaces, from oldest to newest.
The JSON output contains the newest record fields with the joined `reason`.

## Typed reader

`read_needs() -> list[MachineNeeds]` gives the page the same summaries as the list command.
Each `MachineNeeds` contains `newest: NeedRecord` and `reasons: tuple[str, ...]`.
Its `record()` method returns the newest fields with the joined reason.
The reader reads only the open JSON files in the needs folder.
It does not read completed files or run subprocesses.
A missing folder returns an empty list.
The reader skips files that a concurrent clear moves before the read.

## Clear rule

The rebuild writer records each activation in `generations.log` with five space-separated fields:

```text
<time> <action> <generation-link> <nixos-commit> <store-path>
```

The time uses `%FT%T%z`.
Only `switch` lines can clear a need.
The command removes a final `-dirty` suffix from the logged commit before comparison.
It runs this command for each open need of the selected machine:

```sh
git -C <nixos-repo> merge-base --is-ancestor <needed-commit> <logged-commit>
```

A successful comparison clears the need.
The command adds the exact log line as `cleared_by` and moves the record to `needs/done/`.
A non-ancestor keeps the need open.
When the comparison returns neither 0 nor 1, the command checks both commits with `git cat-file -e <commit>^{commit}`.
These checks run only after a comparison error.
An unknown commit keeps the need open.
Each distinct unknown commit produces one line per clear command:

```text
anomaly: unknown logged commit <commit>
anomaly: unknown need commit <commit> (need <id>)
```

The command does not delete completed records.

Run the natedev clear command on natedev:

```sh
"$HOME/.claude/scripts/lib/py" -m scripts.rebuild.need clear --machine natedev --generations "$HOME/.local/state/rebuild/generations.log" --nixos-repo /etc/nixos
```

Run the Mac clear command on the Mac:

```sh
"$HOME/.claude/scripts/lib/py" -m scripts.rebuild.need clear --machine mac --generations "$HOME/.local/state/rebuild/generations.log" --nixos-repo "$HOME/nixos"
```

Both commands use the selected needs folder on their machine.
The job must supply the selected machine's log and a repository that contains the compared commits.

# /remove_command

`/remove_command <command name> [--dry-run]` removes a command and every part that only it uses.
With `--dry-run`, it reports the impact and stops after step 3.
It deletes nothing, edits no file, runs no test and sends no morty notice.
The report states that this is a dry run and nothing changed.
It is a prompt only, in `commands/remove_command.md`, with no script and no test.
Its first use removed `/ask_a_friend`.

## How it works

The command gives seven steps:

1. **Inventory.** Read the command name and optional `--dry-run` flag from `$ARGUMENTS`.
   A real run after a dry run repeats the inventory from step 1 because the tree can change.
   It does not reuse the dry-run table.
   Search with `git grep -n -i --untracked` for every spelling of the name.
   Include underscores, hyphens, a slash prefix and words with spaces.
   Also search the ignored local settings (`.claude/settings*.json`, `templates/settings*.json`).
   Search each term the command file defines: its scripts folder, policies, targets, config keys, file names and state paths.
   Trace other callers of each policy, helper, CLI subcommand and config mode.
   Search config values and environment setup and cleanup lists, because fixtures can use other names.
2. **Classify.** Give each hit one class:

   | Class | Action |
   | --- | --- |
   | `dedicated` | Delete it. |
   | `embedded` | Remove only the command's part of the shared file. |
   | `shared` | Keep it and name its owner. |
   | `history` | Keep it. |
   | `permission` | Never edit it. |

3. **Report.** Show a table of every hit before any deletion.
   Put an impact summary above the table for every run. Include:

   - the hit count for each class;
   - each owner unit with its file count;
   - the test modules that step 5 would run after a real removal;
   - numbered steps for permission rules to remove by hand, with each rule's file, line and exact text.

   A requested board post holds at most 900 characters.
4. **Remove.** Resolve shared dependencies and move shared fixtures to live values before the config they use is deleted. Move a removed CLI path's tests to the live function they also cover. Keep every assertion that checks live behavior. Rename test names and labels that name the command.
5. **Verify.** Run the tests of every changed module and of its shared callers and importers. Lint every changed Python file. Repeat every search; only `history`, `shared` and `permission` hits stay.
6. **Notify morty.** After the removal commit exists, send morty the command name, the commit hash and the removed support through `send.py --to morty`, for its tracking list. Show the `send.py` result.
7. **Change-Review.** The commit carries `Change-Review: /<command name> is removed | <machine>, any session: type /<command name> | The command is not in the command list`.

## Invariants

- No unit edits permission settings. The command lists each allow and deny rule that names the removed command as a step for the user.
- A dead reference in another unit's file needs no go. The command deletes it, then sends the showrunner one line that names the file. It asks the owner first only for a change to live behavior in that file.
- A non-zero `send.py` exit is an anomaly to report. The notice is not sent again.

## Gotchas

- A test of a removed CLI path can also cover a live function. `/ask_a_friend`'s `side_pane.py open` subcommand tests also covered `side_pane.open()`, which Astra reviews still use.
- A removed config mode can hide in fixtures under another name. The `caller` assignment value and `AGENTS_CALLER_*` environment keys had no `ask_a_friend` text.

---
description: Remove a command and its dedicated support.
---

Find, classify, remove and report a command's full footprint.

**Usage:** `/remove_command <command name>`

1. **Inventory.** Read the command name from `$ARGUMENTS`.
   Search the repository with `git grep -n -i --untracked` for every spelling.
   Also search the ignored local settings: `.claude/settings*.json` and `templates/settings*.json`.
   Include underscores, hyphens, a slash prefix and words with spaces.
   Read the command file.
   Search for each term it defines:

   - its scripts folder;
   - policies and targets;
   - config keys and sections;
   - file names and state paths.

   Read each hit.

2. **Classify.** Assign each hit one class:

   | Class | Meaning | Action |
   | --- | --- | --- |
   | `dedicated` | Only the command uses this file, key, section or test. | Delete it. |
   | `embedded` | A case, row, branch, flag value or sentence belongs to the command inside a shared file. | Remove only that part. |
   | `shared` | Other callers use this part. | Keep it and name its owner. |
   | `history` | A dated plan, record or as-built history section describes the past. | Keep it. |
   | `permission` | An allow or deny rule in a settings file names the command or its files. | Never edit it. |

3. **Report.** Show the caller a table before any deletion.
   Include the path, line, class, action and owner for each hit.
   Give the user a numbered step for each permission rule to remove by hand.
   Include each rule's file, line and exact text.

4. **Remove.** Delete the `dedicated` parts and remove the `embedded` parts.
   Ask the owner before changing a file that another unit owns.

5. **Verify.** Run the tests of every changed module.
   Lint every changed Python file.
   Repeat every search from step 1.
   Confirm that only `history`, `shared` and `permission` hits remain.

6. **Notify morty.** Send the notice after the removal commit exists.
   Use the command name, removal statement, commit hash and one line about the removed support for morty's tracking list:

   ```bash
   python3 ~/.claude/scripts/message/send.py --to morty \
     --from "<session name>" \
     --text "/<command name> is removed. Commit: <hash>. Removed support: <one line>. Add the removal to your tracking list."
   ```

7. **Change-Review.** Include this line for the commit:
   `Change-Review: /<command name> is removed | <machine>, any session: type /<command name> | The command is not in the command list`

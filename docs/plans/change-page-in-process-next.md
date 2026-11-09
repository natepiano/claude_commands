# Change page in-process calls — next items

Approved by sdlc [7862488f] 2026-11-09 as next work, not now.

## A change page verdict starts no Python process for scratch cleanup

- **Target:** `scripts/lib/scratch.py` `clean_cargo_outputs` (line 1049), reached from `change_review.end_row_scratch` on the change page server thread.
- **Need:** a verdict on a row whose scratch checkout has an external Cargo build folder starts `folder_audit.py` as a child Python process. The change page otherwise makes no child Python process for a verdict.
- **Completion condition:** `clean_cargo_outputs` calls the folder audit as a library function with explicit paths, with no cwd, environment or stream change, safe from server threads. A row in `ActionTest.test_a_verdict_removes_the_rows_scratch_folders_either_way` covers an external Cargo build folder and shows the same cleanup with no Python child.
- **Source:** phase 1 code review (adversary and contract).

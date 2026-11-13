# Roles and names

This doc records how the development process names its roles, where each name lives, and the check that stops a retired name from returning.

`docs/plans/sdlc-roles-design.md` holds the duty matrix (D01–D55), the term table, and the role record definition. It is a frozen record of how those answers were reached, so its step logs and the retired baseline measure stay there. It stays in `docs/plans/` because the retired-name check does not read that folder.

## How it works

**Three role meanings.** `scripts/message/roster.py` keeps these apart:

- `Role` is the organizational role, the `RosterEntry.role` value.
- `WorkerAssignment` holds a `staff.WorkerRole`: `impl`, `test`, `fix`, `review`, `astra-designer`, or `astra-reviewer`.
- `Job` is the shown job. A session with no job holds `JobUnassigned`, as `AssignmentUnrecorded` or `AssignmentUnreadable`.

`docs/as-built/session-roster.md` describes the roster.

Staff records a worker's current role through `KnownWorkerRole` and `set_worker_role`.
The agents log keeps dated attribution for usage reports, not the current role.
An addressed reviewer's lens comes from dispatch evidence.

**Names that can change.** `config/names.toml` has `[jobs]` and `[audiences]`. Each stable key holds an ordered list of display names: the current name first, retired names after it. A rename is one edit. `scripts/lib/names.py` is the one loader: `load_names(path)` returns `Names` or a `NamesError`, and `runtime_names()` loads once per process. Record keys, addresses, session names and pane labels keep the key.

**Shown names.** `ShownName` renders `production · unit · job` in roster output, broadcast lines and Codex thread titles. `send.py` resolves the shown sender once, on the sending machine. A herdr tab title is the session name, with no role word and no production.

**Reviewers.** A code reviewer is an ordinary read-only unit worker. `docs/as-built/long-lived-workers.md` describes them.

**Held design roles.** The Astra designer and reviewer per design are held design roles, in `scripts/astra/design_roles.py` and `scripts/astra/astra_work.py`. `docs/as-built/long-lived-workers.md` describes them.

**The retired-name check.** `scripts/production/roles_audit.py check` exits non-zero and names the file and line of each retired name outside a kept context. It also fails on a printed fixed-display label. On a clean tree it prints nothing and exits 0. `check --prose` also prints `role words in sentences: N` and the list.

- `scripts/production/role_terms.toml` holds the terms: `name`, `kind`, `defined_in`, `other_names`, `kept`, `other_meaning_files` and each term's retired and inflected forms.
- `test_repository_sources_have_no_unkept_retired_names` in `test_roles_audit.py` runs the check on repository sources, so each merge gate runs it. It reads no home-state records.
- `parse_terms` validates `kind`, `defined_in` and `other_meaning_files` and keeps none of them.

**Speed of the check.**

1. `extraction_candidate` selects a Python file when a decoded string Constant can hold a finding, or when the file has a `+` join or an f-string.
2. The scan parses every Python file, for syntax diagnostics, in the default process pool with `chunksize=1`. Each scan worker disables the garbage collector.
3. `python_tree` caches one parsed tree per process.

## Invariants

- No change to a live duty or name happens before the user approves it.
- The check reads every source read-only. It sends no message, push or alert, and starts no session.
- Output with candidate selection is byte-equal to full extraction. A parity test uses `check_sources(root, full_extraction=True)`.
- The selector must be a superset of what extraction can report. A bound name hides its value from the selector, so every join selects the file.
- A kept context applies once, in its named file only. Kept text that is changed or copied to another file is flagged.
- Tests use fixtures, and never read the real `~/.local/state`, memory folders or transcripts.

## Calibration and gotchas

- On natedev, `check` takes about 1.4 s wall and 7.5 s CPU at load 51, and 5.13 s wall at load 150. Wall time follows load, so record the load with each time.
- A reword of a file with a kept context fails the repository test until that file's kept text in `role_terms.toml` is refreshed.
- Stored record fields and addresses that hold a retired word are kept contracts. `role_terms.toml` lists each one, and a change to one adds a kept context in the same change.
- `banned_words_lib.py --diff` waits on stdin unless run with `</dev/null`.
- `Job` values equal their first display name, so key and display text look the same. Code uses the key.
- The broadcast line is a contract with its readers: `zone.py` counts ` - sent\b`, and the Codex winddown tests parse it. Change them together.
- A config edit does not retitle a live tab. Fixtures pass a config path or patch `runtime_names`, and never read the live config.

## Why

- **A merge-gate check, not a periodic review.** A retired name that returns fails at once, in the change that brought it back.
- **Per-file kept contexts.** Global path masking would hide a returning name in a kept file.
- **Explicit inflected forms.** A general English plural rule would match words with other meanings.
- **No count in a plain check.** The sentence count is not a finding, and it sets a floor of about 2.5 s.
- **The baseline measure is gone.** It served only the design work and its report, which the user dropped.

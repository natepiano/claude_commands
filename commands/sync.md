---
description: Read the cargo-berth board, claim paths named directly, check phase drift, and operate reservation sequencing and integration without teaching the engine any plan format.
---

# Sync

`/sync` reads the cargo-berth board, claims paths named directly, checks phase
drift, and operates reservation sequencing and integration. `cargo-berth` never
reads Markdown, phases, or repository-specific structure.

Use the installed executable on `PATH` only. Every invocation calls the engine
directly. Never build, install, or retry the binary here.

## The invocation pattern

Every command below runs this pattern once, substituting its own engine
arguments. Run it as one Bash call so the engine runs once and its exit code
survives:

```sh
cd "$(git rev-parse --show-toplevel)" || exit 1
envelope="${TMPDIR:-/tmp}/berth-envelope-$$.json"
CARGO_BERTH_SESSION_ID="$CLAUDE_CODE_SESSION_ID" \
  cargo-berth <engine arguments> --json >"$envelope"
status=$?
jq -r '(.presentation.blocks // []) as $blocks
       | if ($blocks | length) == 0 then .message
         else ($blocks
               | map(if (.detail // "") == "" then .summary
                     else .summary + "\n\n" + .detail end)
               | join("\n\n"))
         end' "$envelope"
printf 'engine exit: %s\n' "$status"
```

Three parts of it are not optional:

- **`cd` to the repository top level.** `/sync` may be reached from any
  directory inside the repository; the engine is invoked from its root. A
  directory outside a Git repository is an error and stops the command.
- **`CARGO_BERTH_SESSION_ID` from `CLAUDE_CODE_SESSION_ID`.** The engine reads
  the coordination identity from its own variable, and the harness publishes the
  session under a different one. An invocation missing it is a different actor.
- **The exit code is captured before anything else runs.** The whole
  authorization state machine below is phrased over exit codes, and a pipeline
  reports the exit of its last stage, so `cargo-berth … | jq` would report `jq`'s
  success for every engine refusal.

The `jq` filter is transcription, never classification: it prints the blocks the
engine already rendered, joined as the engine composed them, and falls back to
the envelope's own message when a response carries no blocks. It reads no
status, verb, or payload vocabulary, so a binary reporting something this
installation has never heard of still prints. That is what makes installing a
new binary the entire repair. Do not add wording, ordering, or interpretation to
what it prints.

`"$envelope"` holds the full response for the few commands below that name a
field. Read those fields from it with `jq`; never re-derive from them anything
the rendering already states.

A repository that has not run `cargo-berth init` reports `unconfigured` and every
command stops there, so this command is safe to reach from any repository.

## Commands

### `/sync board`

Engine arguments: `board`

Never invoke bare `board` without `--json`: it can open the full-screen terminal
or print only a pointer. Show the rendering. It includes every envelope field,
every field under `payload.data`, every additive alert, and a separate list of
every `action`, `instruction`, `flag`, `flags`, `resolve_flag`, and `resolution`
value. Do not drop a field or user action.

`recovered_bypasses_this_invocation` is a one-read notice: show it on the read
that adopted the pending marker. It is empty on the next read, while the durable
entry remains under `bypass_audit`. Do not describe disappearance of the notice
as disappearance of the audit fact.

### `/sync claim <file:path|tree:path>...`

Name one or more repository-relative paths directly. `--plan <plan> --phase
<phase>` may be supplied together to retain Work Plan provenance; omit both for
an explicit source. A reservation over the paths a phase edits now comes into
being on first touch, so an explicit claim is for someone who wants to reserve
paths ahead of touching them.

Engine arguments: `claim <file:path|tree:path>...`

The command has one authorization state machine:

1. **Blocked.** A neutral claim returning exit `1` is a refusal. Show the
   rendering; it is the shared refusal rendering used by the `PreToolUse` hook.
   It names every holder's reservation id, run id, branch or detached head, claim
   time, purpose, exact shared scopes, and activity as either active or gone
   quiet with its last activity time. It renders a `work_plan` source with plan
   and phase, an `explicit` source as an explicit claim with no plan or phase,
   and a `first_touch` source as a first-touch claim with no plan or phase. Never
   print an empty plan/phase field or turn `first_touch` into `explicit`.

   The rendering already names the scopes the refusal concerns, and which holder
   each one is shared with. Show it as written; do not reconstruct that from the
   envelope's facts and do not describe the scopes in your own words.

   Where a holder's source is `first_touch`, the rendering carries that holder's
   own `release`, `integrated_as`, and `abandon` commands. Show them as written;
   do not compose those command lines yourself. None of the answers below clears
   a first-touch holder: it was acquired by whichever edit reached the paths
   first, so it may protect no work, and only its own holder disposes of it.
   `release` must run from the holder's worktree; both `resolve` dispositions run
   from anywhere but assert facts about the holder's work, so the requester asks
   the holder rather than recording one.

   Ask the user for exactly one answer for one holder. Answers 1 through 4
   require a non-empty reason and are flags on `claim`, never separate verbs. The
   rendering carries the menu the engine wrote; present those answers as it
   states them rather than from a list held here.

   1. **Land before the holder** — engine arguments gain `--before
      <reservation-id> --overlap-why "<the user's non-empty reason>"`. The
      requester takes the paths and integrates first; the holder is held until
      the requester is on trunk. Use when the holder will build on this
      requester's change.
   2. **Land after the holder** — engine arguments gain `--after
      <reservation-id> --overlap-why "<the user's non-empty reason>"`. The
      requester takes the paths and integrates second; it remains held until the
      holder's protected tip is on trunk and is an ancestor of the requester's
      `HEAD`. Use when the requester will build on the holder.
   3. **Defer the order** — engine arguments gain `--defer <reservation-id>
      --overlap-why "<the user's non-empty reason>"`. The requester takes the
      paths, no ordering edge is added, and the unresolved overlap stays visible
      on `cargo-berth board --json` until someone later sequences it.
   4. **Override** — engine arguments gain `--override <reservation-id>
      --overlap-why "<the user's non-empty reason>"`. The requester takes the
      paths, no ordering edge is added, and the override plus its reason stays
      visible on `cargo-berth board --json`.
   5. **Leave it alone.** Run no engine command, append nothing, and work
      elsewhere.

   Only `before` and `after` add an ordering edge. `defer` and `override` do not;
   the board record is the durable proof that the answer was recorded. The
   trunk-gate bypass is not an edit answer and cannot permit the blocked edit.

   An agent never chooses an answer, invents its reason, or answers its own
   block. If more than one blocker remains, require the user to narrow the
   requested scopes; a proposal binds exactly one named blocker.

2. **Proposal awaiting approval.** After the user supplies the answer, blocker,
   and reason, run one answered invocation, for example:

   ```sh
   cd "$(git rev-parse --show-toplevel)" || exit 1
   envelope="${TMPDIR:-/tmp}/berth-envelope-$$.json"
   CARGO_BERTH_SESSION_ID="$CLAUDE_CODE_SESSION_ID" \
     cargo-berth claim <file:path|tree:path>... \
     --after <reservation-id> \
     --overlap-why "<the user's non-empty reason>" --json >"$envelope"
   status=$?
   ```

   Exit `3` is a proposal awaiting approval. Show the rendering; it includes
   every holder fact from the refusal, the exact shared scopes, selected
   direction, reason, consequence, proposal, and transient token, but not the
   five-answer menu. A proposal binds exactly one displayed conflict, and its
   consequence must agree with its selected answer. Read the token from the
   retained envelope with
   `jq -r '.payload.data.proposal_token' "$envelope"`, retain it only as
   transient conversation state, and ask for approval of the answer already
   selected; do not ask for a different answer. Stop. Issuing a token and
   spending it in the same turn is never permitted.

3. **Claimed.** Only after a later, explicit approval, repeat the same answered
   invocation with the exact token appended:

   ```sh
   cd "$(git rev-parse --show-toplevel)" || exit 1
   envelope="${TMPDIR:-/tmp}/berth-envelope-$$.json"
   CARGO_BERTH_SESSION_ID="$CLAUDE_CODE_SESSION_ID" \
     cargo-berth claim <file:path|tree:path>... \
     --after <reservation-id> \
     --overlap-why "<the same user-supplied reason>" \
     --proposal <exact-token> --json >"$envelope"
   status=$?
   ```

   Exit `0` is a successful claim. Show the rendering and return
   `reservation_id` explicitly, read from the retained envelope with
   `jq -r '.payload.data.reservation_id' "$envelope"`.
   If `session_mapping_publication.status = unavailable`, this is degraded
   success: the journal and reservation are durable, so report its diagnostic,
   continue, and name the reservation id explicitly from then on.

A token-bearing exit `3` is stale: discard the old token, render the refreshed
proposal, and require a new explicit approval. Exit `1` means the named conflict
changed: return to **Blocked** with current facts. Exit `5` is malformed input,
not staleness; discard the token, show the structured invalid-input diagnostic,
and restart from an answered invocation without a token. Never apply a token in
the turn that selects an answer or writes a reason.

`--proposal` without one of `--before`, `--after`, `--defer`, or `--override` is
refused by the engine's own command line at exit `5`, before any ledger read.

`/plan:delegate` uses first-touch claiming at its edit boundary and does not
reproduce proposal logic or read a second parser.

### `/sync check`

This answers "did anything stray outside what was claimed?" Run drift's full
phase-start comparison explicitly.

Engine arguments: `drift --full`

`--full` is mandatory. The engine's fingerprint path issues three `git diff`
commands and one `git ls-files` command. Do not use drift's cheap default, whose
`status` plus `ls-files` comparison answers only whether anything changed since
the last observation.

The engine's `check` verb remains reachable for its different question — whether
a proposed path or edit collides with live reservations:

- `/sync check-path <file:path|tree:path>...` — engine arguments:
  `check <paths...>`
- `/sync check-edit` uses the same `check` invocation for paths an edit would
  touch.

Never pass a run id to `check`; the engine resolves edit authorization itself.
Show the rendering either way. On a blocked check it is the same five-answer
refusal shown by `/sync claim` and the edit hook, naming the scopes it concerns;
on a clear check it says what was authorized and how. The engine states what a
reader sees, so print that text and add nothing to it — do not classify the
outcome, name the acquisition, or reconstruct any of it from the envelope.

### `/sync release`, `/sync sequence`, and `/sync integrate`

Map directly to the phase-1 verbs. Engine arguments:

- `release <reservation-id>`
- `sequence <first-reservation-id> <then-reservation-id> --why "<user reason>"`
- `integrate <reservation-id>`

Do not silently choose `integrate --force`; it skips recorded holds and requires
the user's explicit direction and reason.

If `sequence` or `integrate` rejects an `inactive_session_mapping`, say that the
harness session names an inactive coordination run, name the run and requested
reservation(s), and tell the user to restart the coordination run or use the
explicit active reservation. Do not call it a stale worktree marker;
`inactive_marker_run` is a different rejection: name its coordination run and
tell the user to remove or replace the stale worktree coordination marker.

## Terminal outcomes

These rules bind every command before its ordinary transition:

- `unconfigured` at exit `4` with `no_facts` is terminal. Print the exact
  expected `.claude/config/berth.toml` path from the diagnostic and say to run
  `cargo-berth init`. Do not enter board, claim, release, sequence, integrate, or
  drift logic.
- `ledger_unreadable` at exit `4` is a genuine ledger/configuration failure, not
  `unconfigured`; report its diagnostic and establish no facts.
- Exit `6` means the engine's single ten-second deadline is exhausted. Report
  "the ledger is busy, try again" and name the exact command to rerun. Do not
  retry, clear, or describe the ledger as unreadable.
- Exit `5` is invalid input. Exit `7` belongs to the terminal board and is
  unreachable because `/sync board` always uses `--json`.

State is pulled when the user asks. There is no emit ritual.

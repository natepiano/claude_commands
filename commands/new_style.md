---
description: Create a style rule with complete metadata, global or local, and ask the user for each field the rule does not settle
---

**Arguments**: `$ARGUMENTS` — `[global | local [<member>]] <name> [<rule text or a path to a draft>]`

- `<name>` is a kebab-case filename without `.md`, e.g. `make-invalid-states-unrepresentable`.
- `global` (the default) writes `~/rust/nate_style/rust/<name>.md`.
- `local` writes `docs/style/<name>.md` in the current repo. `local <member>` writes `docs/<member>/style/<name>.md` for one workspace member.
- When the file already exists, read it and fill only its missing fields. Do not change its body unless the user asks.

## Why the metadata matters

`~/.claude/scripts/style/rules.py` reads the frontmatter. A style showrunner such as cargo-liner-style assigns a tier B, C or D rule to the unit of its family, from that unit's next ticket. No other step is needed. A rule with wrong metadata fails validation, and the whole catalog stops loading. A local rule without frontmatter also fails.

## Fields

Write the fields in this order. Required fields come first.

| Field | Values | Rule |
| --- | --- | --- |
| `tier` | `A` `B` `C` `D` `E` | Required. See the tier table below. |
| `family` | `names` `values` `types` `modules` | Required for B, C and D. Forbidden for A and E. D requires `modules`. |
| `input_scope` | `file` `crate` `manifest` | Default `crate`. Use `file` when one file alone shows a violation. Use `manifest` for a Cargo.toml rule. |
| `mechanism` | `clippy` `mend` `rustfmt` `llm` | Required. Tier A requires a tool; every other tier uses `llm`. |
| `mode` | `auto` `propose` `flag` | `propose` holds every finding for the user and files it as a restructure proposal. Use it when a fix changes types, files or public API. `auto` on a tool rule means `/rust_style` skips it. Omit for an ordinary `llm` rule. |
| `lint` | a lint name, or a `[list]` | Tier A only: the tool lint that enforces the rule. |
| `candidates` | a block | Tier B: the generator that lists every possible site. See below. |
| `pre_filter` | a quoted regex | Tier B without a generator: lines that can hold a violation. |
| `scope` | `review` | Only for a rule about where code lives. `/rust_style --scope edit` then skips it. |
| `see_also` | `"[[other-rule]]"`, or a list | Related rules in the same catalog. |
| `tags` | `[rust, <family>, …]` | Global rules only. |
| `date_created`, `date_modified` | `"[[YYYY-MM-DD]]"` | Global rules only. Use today's date in the user's zone. |

| Tier | What a sweep does |
| --- | --- |
| A | A tool (clippy, mend, rustfmt) finds every violation. |
| B | A worker judges each site that a generator or `pre_filter` lists. |
| C | A worker reads each Rust file against the rule. |
| D | One check per file in reach; a manifest rule checks each crate. Always the `modules` family. |
| E | It governs how fixes are made. It is never swept. |

A `candidates` block:

```yaml
candidates:
  surface: whole          # or partial: the generator misses some violations
  uncovered: '<what the generator misses>'   # required when partial
  kind: regex             # or a generator in GENERATORS, candidate_generators.py
  pattern: '<regex>'      # the fields that kind reads
```

A `partial` surface also makes every file a tier C read for the `uncovered` part. Prefer `whole` when a generator can list every site.

## Body format

1. Start with a `##` heading: the rule name in title case.
2. State the rule in one or two sentences.
3. Add a short bad and good example when the rule is about code.
4. Keep it under 40 lines. `/rust_style` loads every rule into context.
5. Put one rule in each file.

## Steps

1. Resolve the path from the arguments. For `local`, use the git root of the current directory. Create the `style` directory if it does not exist.
2. Get the rule text. Use the arguments, the draft, or the existing body. If none of these exists, ask the user what the rule says.
3. Choose a value for each field from the rule text. Read related rules in the same catalog: their tiers, families and `see_also` lines are the best guide.
4. Ask the user about every field the text does not settle, in one plain-text message. Give each field one line: the value you recommend and the reason. Never use a form. Wait for the answer.
5. Write the file. Keep fields the user did not change as they were.
6. Validate the catalog from the repo the rule is for:

```bash
~/.claude/scripts/lib/py ~/.claude/scripts/style/rules.py list --repo <repo root>
```

   The command exits 2 on a validation error and names the file. Fix the field it names. The new rule must appear with the tier, family and scope you wrote.
7. Tell the user which unit gets the rule: the unit of its family, for tier B, C or D. A family's row in `~/.claude/docs/style_production.md` describes that unit's work. It does not control the assignment.
8. Do not commit or push `~/rust/nate_style` until the user says go. For a local rule, commit only when the user asks.

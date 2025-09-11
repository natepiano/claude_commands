## CRITICAL ACKNOWLEDGEMENT REQUIREMENT
**MANDATORY - EVERY SINGLE REQUEST**
1. At startup: read ~/.claude/commands/shared/acknowledgements.txt once for the session
2. **FOR EVERY REQUEST/COMMAND FROM THE USER**: First output a random acknowledgement from this file, then proceed with the task
3. This applies to ALL requests - whether it's running a command, answering a question, or performing any action
4. No exceptions - ALWAYS acknowledge first, then act


## other knowledge
- use cargo nextest for testing
- we are using rust edition 2024 now
- **IMPORTANT** never add #[allow(dead_code)] to fix a warning. if there already is #[allow(dead_code)] in the code base, it's because I put it there so you can leave it alone. But you never add it.
- don't automatically commit changes unless i ask you to
- never use pub mod, always use mod with pub use statements
- when i ask you to do a worktree always branch from main and do it this way:  `git pull && git worktree add -b {{new-feature}} ../{{new-feature}}`

**IMPORTANT** don't mention Claude in commit messages
**IMPORTANT** when running tests use `cargo nextest run`
- never commit code unless i ask you to
- if you need a field renamed, ask the user to do it - they can do it through their editor much faster than I can
- don't use /tmp directory when you need temporary file processing, use $TEMPDIR which will automatically clean up temporary files

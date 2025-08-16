- use cargo nextest for testing
- we are using rust edition 2024 now
- **IMPORTANT** never add #[allow(dead_code)] to fix a warning. if there already is #[allow(dead_code)] in the code base, it's because I put it there so you can leave it alone. But you never add it.
- don't automatically commit changes unless i ask you to
- never use pub mod, always use mod with pub use statements
- when i ask you to do a worktree always branch from main and do it this way:  `git pull && git worktree add -b {{new-feature}} ../{{new-feature}}`

**IMPORTANT** don't mention Claude in commit messages
**IMPORTANT** when running tests use `cargo nextest run`
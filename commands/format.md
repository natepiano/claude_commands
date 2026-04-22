---
description: Run taplo fmt and cargo +nightly fmt unsandboxed
---

Run the following two commands using `dangerouslyDisableSandbox: true` on each Bash tool call. Both must run unsandboxed (taplo panics in the sandbox; nightly toolchain invocations also need unsandboxed execution).

1. `taplo fmt`
2. `cargo +nightly fmt`

Run them sequentially (taplo first, then cargo). Report the results to the user.

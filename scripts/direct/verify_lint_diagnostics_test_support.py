"""Real stderr from the installed Rust 1.99.0 compilers, captured 2026-10-31.

Each command reads the stated one-line source on stdin and exits 1. These are
saved diagnostics; the tests do not invoke compilers or build Cargo packages.
"""

# clippy-driver - --emit=metadata -o /dev/null -D warnings
# stdin: #![deny(clippy::needless_return)] fn main() { return; }
CLIPPY_DENY_ATTRIBUTE = """error: unneeded `return` statement
 --> <anon>:1:47
  |
1 | #![deny(clippy::needless_return)] fn main() { return; }
  |                                               ^^^^^^
  |
  = help: for further information visit https://rust-lang.github.io/rust-clippy/rust-1.99.0/index.html#needless_return
note: the lint level is defined here
 --> <anon>:1:9
  |
1 | #![deny(clippy::needless_return)] fn main() { return; }
  |         ^^^^^^^^^^^^^^^^^^^^^^^
help: remove `return`
  |
1 - #![deny(clippy::needless_return)] fn main() { return; }
1 + #![deny(clippy::needless_return)] fn main() {}
  |

error: aborting due to 1 previous error

"""

# rustc - --emit=metadata -o /dev/null -D warnings
# stdin: #![deny(unused_variables)] fn main() { let unused = 1; }
RUSTC_DENY_ATTRIBUTE = """error: unused variable: `unused`
 --> <anon>:1:44
  |
1 | #![deny(unused_variables)] fn main() { let unused = 1; }
  |                                            ^^^^^^ help: if this is intentional, prefix it with an underscore: `_unused`
  |
note: the lint level is defined here
 --> <anon>:1:9
  |
1 | #![deny(unused_variables)] fn main() { let unused = 1; }
  |         ^^^^^^^^^^^^^^^^

error: aborting due to 1 previous error

"""

# clippy-driver - --emit=metadata -o /dev/null -D warnings
# stdin: fn main() { return; }
CLIPPY_DENY_WARNINGS = """error: unneeded `return` statement
 --> <anon>:1:13
  |
1 | fn main() { return; }
  |             ^^^^^^
  |
  = help: for further information visit https://rust-lang.github.io/rust-clippy/rust-1.99.0/index.html#needless_return
  = note: `-D clippy::needless-return` implied by `-D warnings`
  = help: to override `-D warnings` add `#[allow(clippy::needless_return)]`
help: remove `return`
  |
1 - fn main() { return; }
1 + fn main() {}
  |

error: aborting due to 1 previous error

"""

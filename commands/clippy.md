execute the following instructions:

run `cargo clippy --workspace --all-targets --all-features -- -D warnings`

make a todo list for each item returned by clippy.  for each item add the following to the todo list:

<ClippyTodos>
- [ ] fix each issue (or group of tightly related issues in the same function/struct)
- [ ] if the issues is a code change, run `cargo build` to make sure it compiles before moving to the next fix - with the excpetion that there is no need to run `cargo build` if the issue is a doc / comment change
</ClippyTodos>

**Important**
do not fix warnings by marking code as dead - remove dead code
do not fix warnings by prefixing an argument or variable with a _ - remove it if it's not used

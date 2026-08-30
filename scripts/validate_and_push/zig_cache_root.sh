# zig_cache_root.sh — cache root for the zig cross-compile wrappers.
#
# Sourced by zig-linux-cc, zig-linux-cxx, and zig-linux-ar. cargo is the usual
# caller: validate_ci.sh installs the wrappers as CC/CXX/AR/linker for
# x86_64-unknown-linux-gnu and exports both variables below, so the cache lands
# in the repo's target directory alongside the rest of the build output.
#
# Any other caller falls through to the shared user cache, next to the Linux
# sysroot download cache that ensure_linux_sysroot.sh keeps there. The fallback
# must never be the wrappers' own directory: these scripts live inside a git
# repo, so a cache written beside them shows up as untracked work.

zig_cache_root() {
  if [ -n "${VALIDATE_TARGET_DIR:-}" ]; then
    printf '%s\n' "${VALIDATE_TARGET_DIR}"
  elif [ -n "${CARGO_TARGET_DIR:-}" ]; then
    printf '%s\n' "${CARGO_TARGET_DIR}"
  elif [ "$(uname -s 2>/dev/null || true)" = "Darwin" ] && [ -n "${HOME:-}" ]; then
    printf '%s\n' "${HOME}/Library/Caches/validate-and-push/zig"
  elif [ -n "${XDG_CACHE_HOME:-}" ]; then
    printf '%s\n' "${XDG_CACHE_HOME}/validate-and-push/zig"
  elif [ -n "${HOME:-}" ]; then
    printf '%s\n' "${HOME}/.cache/validate-and-push/zig"
  else
    printf '%s\n' "/tmp/validate-and-push/zig"
  fi
}

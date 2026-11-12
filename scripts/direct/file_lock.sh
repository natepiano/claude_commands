# Lock an open descriptor with Perl on Linux and macOS.
lock_file_descriptor() {
  local perl candidate
  for candidate in "$(command -v perl)" /run/current-system/sw/bin/perl /usr/bin/perl; do
    [[ -n "$candidate" && -x "$candidate" ]] && { perl="$candidate"; break; }
  done
  [[ -n "${perl:-}" ]] || return 2
  "$perl" -e 'use Fcntl ":flock";
open my $guard, ">&=", $ARGV[0] or exit 2;
flock($guard, LOCK_EX | ($ARGV[1] eq "nonblocking" ? LOCK_NB : 0))
    or exit($!{EWOULDBLOCK} || $!{EAGAIN} ? 75 : 2);' "$1" "${2:-blocking}"
}

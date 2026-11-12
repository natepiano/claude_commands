#!/usr/bin/env zsh
# Exit 0 while a production runs, 2 when it has wrapped, 1 otherwise.

setopt no_unset pipe_fail extended_glob
(( $# == 1 )) || exit 1
doc=$("${0:A:h:h}/lib/py" "${0:A:h}/showrunners.py" doc "$1") || exit 1

for line in "${(@f)$(<$doc)}"; do
  if [[ $line == *'Status: PRODUCTION — '* ]]; then
    doc_status=${${line#*'Status: PRODUCTION — '}%%[^a-z]*}
    case $doc_status in
      running)
        # Copy a changed LOG during this check. A failed copy does not change the result.
        "${0:A:h:h}/lib/py" "${0:A:h}/log_backup.py" "$1" >/dev/null || true
        exit 0 ;;
      wrapped) exit 2 ;;
      *) exit 1 ;;
    esac
  fi
done
exit 1

# The agent registry as two files: config/agents.conf holds the committed
# defaults, and config/agents.local.conf (untracked) holds only the rows of this
# machine that differ. A row is `key=value` inside `[section]`; `#` starts a
# comment. POSIX awk only: the Mac runs BSD awk.
#
#   awk -v mode=view   -f agents_overlay.awk <overlay> <defaults>
#       The defaults with each overlay row in place of the default row of the
#       same section and key, and every overlay row the defaults lack appended
#       to its section (a section the defaults lack is appended at the end).
#   awk -v mode=store  -f agents_overlay.awk <defaults> <registry>
#       The rows of <registry> that differ from the defaults or that the
#       defaults lack: the overlay that, viewed over the defaults, gives them.
#   awk -v mode=rename -f agents_overlay.awk <registry>
#       <registry> with the names from before the unit rename moved to the new
#       ones: [delegate.*] sections become [direct.*], and `delegate` keys
#       under [assignments] become `direct`.

function trim(text) {
    sub(/^[[:space:]]+/, "", text)
    sub(/[[:space:]]+$/, "", text)
    return text
}

# Parse $0 into kind ("section", "row" or "other"), name, key and value.
function parse(    text, equals) {
    text = $0
    sub(/#.*/, "", text)
    text = trim(text)
    kind = "other"
    if (text ~ /^\[.+\]$/) {
        kind = "section"
        name = substr(text, 2, length(text) - 2)
    } else if (index(text, "=") > 0) {
        kind = "row"
        equals = index(text, "=")
        key = trim(substr(text, 1, equals - 1))
        value = trim(substr(text, equals + 1))
    }
}

# Print the overlay rows of `section` the defaults did not have.
function flush(section,    i, id) {
    for (i = 1; i <= row_count; i++) {
        id = row_ids[i]
        if (row_section[id] == section && !(id in used)) {
            print row_key[id] "=" overlay[id]
            used[id] = 1
        }
    }
}

FILENAME == ARGV[1] && mode == "view" {
    parse()
    if (kind == "section") {
        current = name
        if (!(current in section_seen)) {
            section_seen[current] = 1
            sections[++section_count] = current
        }
    } else if (kind == "row" && current != "") {
        id = current SUBSEP key
        if (!(id in overlay)) row_ids[++row_count] = id
        overlay[id] = value
        row_section[id] = current
        row_key[id] = key
    }
    next
}

FILENAME == ARGV[1] && mode == "store" {
    parse()
    if (kind == "section") current = name
    else if (kind == "row" && current != "") defaults[current SUBSEP key] = value
    next
}

mode == "view" {
    parse()
    if (kind == "section") {
        if (started) flush(current)
        started = 1
        current = name
        in_defaults[current] = 1
        print
    } else if (kind == "row" && current != "" && ((current SUBSEP key) in overlay)) {
        id = current SUBSEP key
        print key "=" overlay[id]
        used[id] = 1
    } else {
        print
    }
    next
}

mode == "store" {
    parse()
    if (kind == "section") {
        current = name
    } else if (kind == "row" && current != "") {
        id = current SUBSEP key
        if (!(id in defaults) || defaults[id] != value) {
            if (!(current in printed)) {
                if (printed_count++ == 0) {
                    print "# This machine's rows over config/agents.conf, written by agents_config.sh."
                    print "# Untracked: a commit or checkout never touches them."
                }
                print ""
                print "[" current "]"
                printed[current] = 1
            }
            print key "=" value
        }
    }
    next
}

mode == "rename" {
    parse()
    if (kind == "section") {
        current = name
        if (name ~ /^delegate\./) {
            print "[direct." substr(name, length("delegate.") + 1) "]"
            next
        }
    } else if (kind == "row" && current == "assignments" && (key == "delegate" || key ~ /^delegate\./)) {
        print "direct" substr(key, length("delegate") + 1) "=" value
        next
    }
    print
    next
}

END {
    if (mode == "view") {
        if (started) flush(current)
        for (i = 1; i <= section_count; i++) {
            if (sections[i] in in_defaults) continue
            print ""
            print "[" sections[i] "]"
            flush(sections[i])
        }
    }
}

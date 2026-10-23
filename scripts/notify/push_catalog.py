"""Script phone texts and named cases reviewed before delivery."""
from push_texts import ActionTemplate, NoActionNeeded, PushExample, PushText, SessionTemplate

# Codex usage destination: https://learn.chatgpt.com/docs/pricing (read 2026-10-14).
# Real quota case: notify/review.jsonl, 2026-10-13T22:30:22.569264-04:00.
# Other script senders have no live record in the two supplied logs. Their
# typical cases use existing test facts or the Nix thresholds, never invented log citations.


ENTRIES = (
    # Stall-watch examples are the real prompts of 2026-10-23: a Bash call reads "run <command>" (cut to 89
    # characters and "…"), an edit "edit <file>", as stall_watch.prompt_request words them from the pending call.
    PushText("stall_watch.permission_prompt", "stall-watch", "blocked", "{unit} needs your answer",
        ActionTemplate("On {machine}, open the {unit} herdr tab and answer its prompt"),
        "{unit} asks to {request}. It has waited since {since}.\n\n"
        + "Yes: it does that and goes on.\n\n"
        + "No: it does not, and asks you what to do instead.",
        (PushExample("typical", {"unit": "handler", "machine": "natedev", "since": "09:01 AST",
            "request": "run cargo build -p cargo-handler"}),
         PushExample("edit", {"unit": "messaging", "machine": "natedev", "since": "06:05 AST",
            "request": "edit send.py"})),
        purpose="A session waits for you to answer its permission prompt.",
        session=SessionTemplate("{unit}")),
    PushText("shutdown.holdout", "shutdown", "decision", "{session} {operation} is blocked",
        ActionTemplate("answer the open form"),
        "Reason: {operation} is blocked because {session}'s claude session is showing a form and it can't proceed.\n\n"
        + "Answer the form (recommended): the session pauses, and the {operation} goes on.\n\n"
        + "Stop the session: it ends, its uncommitted work stays, and the {operation} goes on without it. "
        + "In a terminal on {machine}, run:\n\n{command}",
        (PushExample("typical", {"session": "pm-move-ghostty", "machine": "natedev",
            "operation": "restart",
            "command": "~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py now claude4 --here --only d5b35898-290f-4b44-8e6f-6f2586c975a8"}),),
        purpose="A shutdown or restart waits for a session that shows a form.",
        session=SessionTemplate("{session}")),
    PushText("shutdown.holdout_unanswered", "shutdown", "decision", "Your Claude {operation} is stuck",
        ActionTemplate("In a terminal on {machine}, stop {session} with the command below"),
        "Your Claude {operation} waits for each session to pause. The session {session} has not paused, so the {operation} is stuck.\n\n"
        + "Stopping it ends only that session and keeps its uncommitted work. Then the {operation} goes on.\n\n{command}",
        (PushExample("typical", {"session": "pm-move-ghostty", "machine": "natedev",
            "operation": "restart",
            "command": "~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py now claude4 --here --only d5b35898-290f-4b44-8e6f-6f2586c975a8"}),
         PushExample("long_session", {"session": "build-followups-director", "machine": "Mac",
            "operation": "shutdown",
            "command": "~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py now claude2 --here --only 5b3a1624-bb7e-46af-8cb1-4d960d719a32"})),
        purpose="A shutdown or restart is stuck on a session that did not pause.",
        session=SessionTemplate("{session}")),
    # Shutdown examples use test_settle.py and test_restart.py facts.
    # The failing shutdown deliveries are recorded in notify/review.jsonl, 2026-10-14T02:05Z.
    PushText("shutdown.stop_failed", "shutdown", "decision", "Claude account shutdown is incomplete",
        ActionTemplate('In the Claude session named natedev on the natedev computer, run /shutdown now {account}.'),
        "The shutdown of Claude account {account} did not finish on {machine}. "
        + "{stopped} did not stop, so work may still be running there. The command above retries the cleanup.",
        (PushExample("typical", {"account": 'claude2', "machine": "natedev", "stopped": "One session"}),
         PushExample("many_long_names", {"account": '"claude-team-build-followups-12"', "machine": "Mac", "stopped": "14 sessions"})),
        purpose="An account shutdown did not stop all of its sessions."),
    PushText("shutdown.restart_failed", "shutdown", "decision", "Claude coding work is still stopped",
        ActionTemplate('In the Claude session named natedev on the natedev computer, run /shutdown restart {account}.'),
        "After your restart request, Claude coding work for account {account} is still stopped on {machine}. "
        + "{stopped} did not restart, so that work stays paused. "
        + "The command above retries the restart on natedev and the Mac.",
        (PushExample("typical", {"account": 'claude2', "machine": "natedev", "stopped": "One session (organon-unit)"}),
         PushExample("one_long_name", {"account": '"claude-team-build-followups-12"', "machine": "Mac",
             "stopped": "One session (build-followups-two-accounts-unit-director)"})),
        purpose="A restart did not start all stopped sessions again."),
    PushText("shutdown.restart_incomplete", "shutdown", "decision", "Claude restart could not be confirmed",
        ActionTemplate('In the Claude session named natedev on the natedev computer, run /shutdown restart {account}.'),
        "The restart of Claude account {account} could not be confirmed on {machine}. "
        + "Its stopped sessions may still be unable to work. Retrying asks any sessions that are still stopped to start again.",
        (PushExample("typical", {"account": 'claude2', "machine": "Mac"}),
         PushExample("long_account", {"account": '"claude-team-build-followups-12"', "machine": "natedev"})),
        purpose="A restart ended, but its result could not be confirmed."),
    PushText("shutdown.manual_restart", "shutdown", "decision", "Stopped Claude coding work needs you",
        ActionTemplate('In the Claude session named natedev on the natedev computer, send: "Resume the stopped Claude session named {session} on {machine} from its shutdown record."'),
        "A Claude coding session on {machine} stopped during your shutdown. Its work is paused. "
        + "Its terminal cannot restart on its own. Its shutdown record holds the command to resume it.",
        (PushExample("typical", {"machine": "natedev", "session": "Owner"}),
         PushExample("long_session", {"machine": "Mac", "session": "build-followups-message-clarity-unit-director-session-12"})),
        purpose="A session stopped in a shutdown cannot restart on its own."),
    PushText("quota.claude_next_move", "quota_alert", "note", "Claude work on {account} may stop",
        ActionTemplate("In the natedev session, type /account_moves and the move you want, for example: when units reach 1% switch to claude2."),
        "{account} has {remaining} of its weekly usage left, and no move is set up for the units or showrunners on it. When it runs out, they move to a Claude account with room, or stop if none has any.",
        (PushExample("uncovered_low", {"account": "claude3", "remaining": "2%"}),
         PushExample("uncovered_empty", {"account": "claude4", "remaining": "0%"})),
        purpose="A Claude account runs low, and no move is set up for it."),
    PushText("quota.codex_next_move", "quota_alert", "note", "{account} Codex workers will {outcome_title}",
        ActionTemplate("Optional: to move them at 1% instead, type in the natedev session: /account_moves when workers reach 1% switch to {fallback}"),
        "{account} has {remaining} of its weekly usage left{outcome_body} Nothing is needed.",
        (PushExample("credits_on", {"account": "codex2", "remaining": "2%", "fallback": "codex1",
                                   "outcome_title": "use paid credits",
                                   "outcome_body": ", and no move is set up for its workers. When it runs out, they spend its paid credits, then move to codex1."}),
         PushExample("credits_off_codex", {"account": "codex2", "remaining": "2%", "fallback": "codex1",
                                         "outcome_title": "move to codex1",
                                         "outcome_body": ". When it runs out, its workers move on their own to codex1, the Codex account with room whose weekly reset comes first."}),
         PushExample("credits_off_claude", {"account": "codex1", "remaining": "1%", "fallback": "Claude",
                                          "outcome_title": "move to Claude",
                                          "outcome_body": ". When it runs out, its workers move on their own to Claude, since no other Codex account has room."})),
        purpose="A Codex account runs low. Its workers move or spend credits soon."),
    PushText('quota.codex_workers_moved', "quota_alert", "note", '{transition}',
        NoActionNeeded(),
        'Codex account {account} has exhausted its included weekly usage until {reset}. Credits are off, so new work moves to the other Codex account, {target}, using {funding}. Jobs already running finish their current turn first.',
        (PushExample('codex_one', {'account': 'codex1', 'reset': 'Tue 2026-10-20 00:58 EDT', 'target': 'codex2', 'funding': '97% weekly usage left', 'transition': 'Codex work now uses codex2'}),
         PushExample('codex_two', {'account': 'codex2', 'reset': 'Thu 2026-10-22 07:00 EDT', 'target': 'codex1', 'funding': 'its own paid credits', 'transition': 'Codex work now spends paid credits'})),
        purpose="A Codex account ran out, and its work moved to another account."),
    PushText('quota.codex_workers_stay', "quota_alert", "note", '{account} {outcome}',
        NoActionNeeded(),
        'Codex account {account} has exhausted its included weekly usage until {reset}. Credits are off. {reason} {fallback}',
        (PushExample('no_room', {'account': 'codex1', 'reset': 'Tue 2026-10-20 00:58 EDT', 'reason': 'No other Codex account has room.', 'outcome': 'work moves to Claude', 'fallback': 'New coding work uses Claude, the other coding assistant, until the earliest Codex refill at Sat 2026-10-17 07:00 EDT. Current turns finish first.'}),
         PushExample('failed_move', {'account': 'codex2', 'reset': 'Thu 2026-10-22 07:00 EDT', 'reason': 'Workers could not move to codex1 because its usage reading is unavailable.', 'outcome': 'work moves to Claude', 'fallback': 'New coding work uses Claude, the other coding assistant, until the earliest Codex refill at Tue 2026-10-20 00:58 EDT. Current turns finish first.'})),
        purpose="A Codex account ran out, and its workers wait or move to Claude."),
    PushText('quota.codex_credits_forecast', "quota_alert", "note", '{account}: paid credits {horizon}',
        NoActionNeeded(),
        '{forecast} {destination}',
        (PushExample('codex_one', {'account': 'codex1', 'forecast': 'Workers on Codex account codex1 used 120 paid credits in the last hour; its 6,370 credits last until about Fri 2026-10-16 13:05 EDT.', 'destination': 'When credits run out, workers move to codex2 (97% weekly usage left).', 'horizon': 'until Fri 13:05 EDT'}),
         PushExample('codex_two', {'account': 'codex2', 'forecast': 'Workers on Codex account codex2 used 40 paid credits in the last hour; its 2,114 credits last until about Fri 2026-10-16 12:51 EDT.', 'destination': 'When credits run out, workers move to codex1 and spend its own paid credits.', 'horizon': 'until Fri 12:51 EDT'}),
         PushExample('zero_use', {'account': 'codex1', 'forecast': 'Workers on Codex account codex1 used no credits in the last hour; 6,370 paid credits left.', 'destination': 'When credits run out, workers move to codex2 (97% weekly usage left).', 'horizon': 'use is zero'}),
         PushExample('unlimited', {'account': 'codex1', 'forecast': 'Codex account codex1 has unlimited paid credits; its coding workers remain on paid usage with no credit limit.', 'destination': '', 'horizon': 'have no limit'}),
         PushExample('unknown_balance', {'account': 'codex1', 'forecast': 'Codex account codex1 reports no credit balance; its coding workers remain on paid usage, but the run-out time is unknown.', 'destination': 'When credits run out, workers move to codex2 (97% weekly usage left).', 'horizon': 'run-out unknown'}),
         PushExample('unknown_use', {'account': 'codex2', 'forecast': 'Coding workers on Codex account codex2 remain on paid usage. Their hourly use is unknown because credit observations cover less than 30 minutes of the hour; 2,114 credits left.', 'destination': 'When they run out, Codex work moves to Claude, the other coding assistant, until Tue 2026-10-20 00:58 EDT.', 'horizon': 'run-out unknown'}),
         PushExample('queued', {'account': 'codex1', 'forecast': 'Workers on Codex account codex1 used 120 paid credits in the last hour; its 6,370 credits last until about Fri 2026-10-16 13:05 EDT.', 'destination': 'When credits run out, workers wait for the account move you queued.', 'horizon': 'until Fri 13:05 EDT'})),
        purpose="Tells when the paid credits of a Codex account run out."),
    PushText("quota.codex_refused", "quota_alert", "decision", "Codex work stopped for lack of usage",
        ActionTemplate("At chatgpt.com/codex/settings/usage, add credits for account {account}."),
        "Codex hit its usage limit on account {account} and stopped accepting coding jobs. "
        + "Moving those jobs to Claude, the other coding assistant, also failed, so the work is paused. "
        + "Adding credits lets Codex accept jobs again.",
        (PushExample("typical", {"account": "codex1"}),
         PushExample("long_account", {"account": "codex999"})),
        purpose="Codex stopped work for lack of usage, and Claude could not take it."),
    # sweep.py sends this only when no idle build output is left and no Claude session holds a busy
    # target; {paths} lists, largest first, the busy targets and then the largest measured folders.
    # Sizes follow the 2026-10-21 disk check on natedev (HANDOFF-builds-unit.md, 00:45 EDT): 26 GiB held in
    # cargo-liner-berth's target, about 86 GiB of scratch clones under /tmp.
    PushText("sweep.disk_floor", "sweep", "note", "Builds need more disk space",
        ActionTemplate("In the Claude session named natedev, name the paths below that it may remove."),
        "The natedev computer has {free} free. Builds need {floor}. They may fail until {shortfall} more is free. "
        + "Build cache cleanup freed {removed} and has no idle build output left.\n\n{paths}\n\n"
        + "The session removes only the paths you name.",
        (PushExample("typical", {"shortfall": "10.0 GiB", "free": "290.0 GiB", "floor": "300.0 GiB", "removed": "2.0 GiB",
            "paths": "/home/natepiano/rust/cargo-liner-berth/target: frees 26.0 GiB. Process 3104822 uses it now and can fail.\n\n"
            + "/tmp/claude-1000/scratch: frees 86.0 GiB."}),
         PushExample("no_free_space", {"shortfall": "300.0 GiB", "free": "0.0 GiB", "floor": "300.0 GiB", "removed": "0.0 GiB",
            "paths": "/tmp/claude-1000/scratch: frees 86.0 GiB.\n\n/home/natepiano/.local/share/Trash: frees 40.0 GiB."})),
        purpose="Builds need more disk space. You name the paths to remove."),
    PushText("rust.passed", "rust_release", "decision", "Hana can use the new Rust compiler",
        ActionTemplate("In the Claude session named natedev, reply upgrade or wait."),
        "Hana, the editor project, passed its compiler check on Rust {version}: no errors. "
        + "It currently uses Rust {pin}.\n\n"
        + "Upgrade (recommended because the check passed): use Rust {version} for Hana's builds.\n\n"
        + "Wait: keep building Hana with Rust {pin}.",
        (PushExample("typical", {"version": "1.100.0", "pin": "1.99.0"}),
         PushExample("long_versions", {"version": "100.100.100", "pin": "99.99.99"})),
        purpose="A new Rust compiler passed its check on Hana. Upgrade or wait."),
    PushText("rust.warnings", "rust_release", "decision", "New compiler adds warnings to Hana",
        ActionTemplate("In the Claude session named natedev, reply wait or upgrade."),
        "Hana, the editor project, passed its code check on Rust {version}, but gained {warnings} warnings. "
        + "Warnings do not prevent builds. It still uses Rust {pin}.\n\n"
        + "Wait (recommended to avoid new warnings): keep Rust {pin}.\n\n"
        + "Upgrade: use Rust {version} with those warnings.",
        (PushExample("typical", {"version": "1.100.0", "pin": "1.99.0", "warnings": "2"}),
         PushExample("many_warnings", {"version": "1.100.0", "pin": "1.99.0", "warnings": "14"})),
        purpose="A new Rust compiler adds warnings to Hana. Upgrade or wait."),
    PushText("rust.errors", "rust_release", "decision", "Rust upgrade failed a code check",
        ActionTemplate("In the Claude session named natedev, reply wait or upgrade."),
        "Rust {version} failed its code check on Hana, the editor project. Errors: {errors}; new warnings: {warnings}. "
        + "Hana still uses Rust {pin}.\n\n"
        + "Wait (recommended to keep builds working): keep Rust {pin}.\n\n"
        + "Upgrade: use Rust {version} despite the errors; Hana's builds may fail.",
        (PushExample("typical", {"version": "1.100.0", "pin": "1.99.0", "errors": "1", "warnings": "2"}),
         PushExample("many_errors", {"version": "1.100.0", "pin": "1.99.0", "errors": "14", "warnings": "14"})),
        purpose="A new Rust compiler failed its check on Hana. Upgrade or wait."),
    PushText("rust.repair_failed", "rust_release", "decision", "Rust upgrade breaks the build repair tool",
        ActionTemplate("In the Claude session named natedev, reply wait or upgrade."),
        "Rust {version} is out. The tool that repairs Rust build errors fails to compile with it. Hana, the editor project, uses Rust {pin}.\n\n"
        + "Wait (recommended): Hana stays on {pin} and keeps build repair. No fix is scheduled.\n\n"
        + "Upgrade: Hana moves to {version}. It may lose build repair. No benefit for Hana is confirmed.",
        (PushExample("typical", {"version": "1.100.0", "pin": "1.99.0"}),
         PushExample("long_versions", {"version": "100.100.100", "pin": "99.99.99"})),
        purpose="A new Rust compiler breaks the build repair tool. Upgrade or wait."),
    PushText("codex.reset_ready", "codex-winddown", "decision", "Codex is ready for your reset",
        ActionTemplate("At chatgpt.com/codex/settings/usage, redeem your limit reset."),
        "All Codex coding agents on {machine}, your computer, have stopped. Redeeming your usage reset now will not interrupt any work. "
        + "New Codex work stays paused until you give the all clear: in a Claude session on {machine}, run /codex_winddown clear.",
        (PushExample("typical", {"machine": "natedev"}),
         PushExample("long_machine", {"machine": "macbook-workstation"})),
        purpose="All Codex agents stopped. You can redeem your usage reset now."),
    # Current disk-floor.nix: alertGib=150, build floor=300 GiB.
    PushText("nixos.disk_low", "disk_floor", "note", "Builds need more disk space",
        ActionTemplate("In a terminal on natedev, delete {shortfall} GiB of personal files you no longer need."),
        "The natedev computer has {free} GiB free on its main disk. Builds need 300 GiB. "
        + "Automated cleanup could not restore that space; builds may fail until you free more.",
        (PushExample("typical", {"free": "149", "shortfall": "151"}),
         PushExample("no_free_space", {"free": "0", "shortfall": "300"})),
        purpose="Cleanup did not free enough disk on natedev. Delete your files."),
    # Current ups.nix: low-runtime shutdown at five minutes or 30% charge.
    PushText("nixos.ups_battery", "ups", "blocked", "Power outage threatens open work",
        ActionTemplate("Save your open work on the natedev computer now."),
        "Mains power is out. The natedev computer is on backup battery and could shut down soon. "
        + "Charge: {charge}%. Time left: about {minutes} minutes. It shuts down at 30% charge. "
        + "It also shuts down at 5 minutes left. This prevents the battery dying while a file is being saved.",
        (PushExample("typical", {"charge": "80", "minutes": "20"}),
         PushExample("shutdown_threshold", {"charge": "30", "minutes": "5"})),
        purpose="Power is out, and natedev runs on battery. Save your work."),
    PushText("nixos.ups_shutdown", "ups", "blocked", "Power outage stopped the computer",
        ActionTemplate("When power returns, start natedev if it stays off for three minutes."),
        "The natedev computer is shutting down because its backup battery is low. "
        + "Work cannot resume until it starts again. It should start automatically when mains power returns.",
        (PushExample("typical", {}),),
        purpose="Power is out, and the battery is low. natedev shuts down."),
    # /nate templates (user spec, 2026-10-20): the sending agent fills each value; the last body line names the template.
    PushText("nate.adhoc", "{sender}", "note", "{sender} {title}", NoActionNeeded(),
        "{description}\n\nadhoc",
        (PushExample("typical", {"sender": "natedev", "title": "the /nate skill is ready",
                                 "description": "You asked to be told when the /nate skill is ready. It is ready now: "
                                 + "/nate adhoc <instructions> sends you an update like this one."}),
         PushExample("long_sender", {"sender": "cargo-liner-style", "title": "renames are done",
                                     "description": "You asked to be told when the tile and handler renames finish. "
                                     + "Both are done, and both units run under their new names."})),
        purpose="An update that an agent writes for you with /nate adhoc.",
        header=False),
    # Auto-mode refusal (user approved 2026-10-21): a session sends it with send.py --text-id auto_mode.refused.
    # send.py fills sender from the session's own name, refused from the command values (one on the Refused
    # line, or several one per line), noun and this from the number of commands, and rule_line from rule.
    PushText("auto_mode.refused", "{sender}", "decision", "{sender}: auto mode stopped {session}",
        ActionTemplate("On {machine}, in the {session} pane, approve the prompt or add an allow rule"),
        "Refused:{refused}\n{waits}.\n\n"
        + "Approve: it goes on now, but {this} {noun} can stop it again.\n\n"
        + "Allow rule: it never stops on {this} {noun} again.{rule_line}",
        (PushExample("typical", {"sender": "cargo-liner-style", "session": "modules", "machine": "natedev",
            "refused": "\nimplement.sh --to modules-impl\nreview.sh --to modules-test",
            "noun": "commands", "this": "these", "waits": "The P542 build waits for them", "rule_line": ""}),
         PushExample("one_with_rule", {"sender": "build-followups", "session": "messaging", "machine": "natedev",
            "refused": " git push origin build-followups-messaging", "noun": "command", "this": "this",
            "waits": "The merge of that work waits for it", "rule_line": " The rule: Bash(git push:*)"})),
        purpose="Auto mode refused a session's commands, and the session waits for you.",
        session=SessionTemplate("{session}")),
    # Permission root cause (user ask, 2026-10-23): rca.py sends these when the owner of a case starts it
    # (`rca.py start`) and when its fix merges (`rca.py fixed`), with --key rca-<case id>. sender is the
    # session's production, or messaging when it has none; commands is one command, "a and b" or "a, b and c".
    # rca.py drops a final period from cause and fix. Examples: the handler and modules cases of 2026-10-23.
    PushText("permission.rca_started", "{sender}", "note", "{sender}: fixing why {session} stops",
        NoActionNeeded(),
        "The {owner} unit finds why {session} asked for {commands} and fixes the source. "
        + "A second push comes when it is fixed.",
        (PushExample("typical", {"sender": "cargo-liner", "session": "handler", "owner": "builds",
            "commands": "cargo build -p cargo-handler"}),
         PushExample("two_commands", {"sender": "cargo-liner-style", "session": "modules", "owner": "style",
            "commands": "implement.sh --to modules-impl and review.sh --to modules-test"})),
        purpose="A unit starts to fix why a session stopped for a permission."),
    PushText("permission.rca_fixed", "{sender}", "note", "{sender}: fixed why {session} stopped",
        NoActionNeeded(),
        "Cause: {cause}.\nFix: {fix} ({commit}).",
        (PushExample("typical", {"sender": "cargo-liner", "session": "handler",
            "cause": "a helper ran a raw cargo build", "fix": "the builds unit added a verify.sh build verb",
            "commit": "5e2b9c41"}),
         PushExample("two_commands", {"sender": "cargo-liner-style", "session": "modules",
            "cause": "a work order told the seat to run the launchers directly",
            "fix": "the work order uses the allowed launcher call", "commit": "a73d0f18"})),
        purpose="The fix merged: the session does not stop for that permission again."),
    # Paid credits (user approved 2026-10-21): sent with send.py --text-id quota.paid_credits. send.py fills
    # sender from the session's own name; a script with no session record names itself with --from.
    # Examples: the codex2 values the user approved, and codex1 sent from the natedev session.
    PushText("quota.paid_credits", "{sender}", "decision", "{sender}: {account} now uses paid credits",
        ActionTemplate("To stop it, set use_credits: off in ~/rust/hanadocs/agents/{account}.md on {machine}"),
        "{account} has {credits} paid credits left. Its plan refills {refill}.\n\n"
        + "Leave on (recommended): work spends these credits, then moves to Claude.\n\n"
        + "Turn off: work moves to Claude until {next_reset}.",
        (PushExample("typical", {"sender": "quota_alert", "account": "codex2", "machine": "natedev",
            "credits": "25,345", "refill": "Tue 10-27 02:33 EDT", "next_reset": "Sat 10-24 16:40 EDT"}),
         PushExample("from_session", {"sender": "natedev", "account": "codex1", "machine": "natedev",
            "credits": "6,370", "refill": "Thu 10-29 07:00 EDT", "next_reset": "Sat 10-24 16:40 EDT"})),
        purpose="An account now spends paid credits. You can turn this off."),
    # Paid credits with a queued workers move (agreed with accounts-unit 2026-10-21): workers never go to
    # Claude; they wait for the queued move. Same title, action, need and sender rule as quota.paid_credits.
    PushText("quota.paid_credits_queued", "{sender}", "decision", "{sender}: {account} now uses paid credits",
        ActionTemplate("To stop it, set use_credits: off in ~/rust/hanadocs/agents/{account}.md on {machine}"),
        "{account} has {credits} paid credits left. Its plan refills {refill}.\n\n"
        + "Leave on (recommended): workers spend these credits until your queued move runs. "
        + "If the credits run out first, workers wait.\n\n"
        + "Turn off: workers stop and wait for your queued move.",
        (PushExample("typical", {"sender": "quota_alert", "account": "codex2", "machine": "natedev",
            "credits": "25,345", "refill": "Tue 10-27 02:33 AST"}),),
        purpose="An account now spends paid credits, and you queued a workers move."),
    # A session waits for a go (user approved 2026-10-21): sent with send.py --text-id session.needs_go.
    # send.py fills sender from the session's own name. Examples: the trunk push the user approved, and the
    # herdr-panes worktree left after herdr-unit took over herdr work (2026-10-18).
    PushText("session.needs_go", "{sender}", "decision", "{sender}: {session} waits for your go",
        ActionTemplate("On {machine}, in the {session} pane, type: {go}"),
        "{why}.\nYour go {does}.",
        (PushExample("typical", {"sender": "natedev", "session": "trunk", "machine": "natedev",
            "go": "Go: remove arrange_jacks/ from phases/130/files; 140 owns it",
            "why": "Steps 130 and 140 both claim the arrange_jacks folder, and your permission check refused the fix",
            "does": "removes it from step 130, and trunk goes on"}),
         PushExample("delete_worktree", {"sender": "build-followups", "session": "herdr", "machine": "natedev",
            "go": "Go: delete the herdr-panes worktree and its branch",
            "why": "The herdr session tried to delete the old herdr-panes worktree, and your permission check refused it. "
            + "The main branch has all its work",
            "does": "deletes them, and herdr starts its next phase"})),
        purpose="A session waits for your go before it does one step.",
        session=SessionTemplate("{session}")),
    # Auto silence (user spec, 2026-10-20: "a very compact format"): auto_silence.py sends one when the
    # user goes idle, before silence starts, and one when their next prompt ends it.
    PushText("silence.auto_on", "{sender}", "note", "{sender} auto silence on", NoActionNeeded(),
        "Idle {minutes} min.",
        (PushExample("typical", {"sender": "natedev", "minutes": "45"}),
         PushExample("long_idle", {"sender": "natedev", "minutes": "1440"})),
        purpose="You went idle, and auto silence starts to keep pushes.",
        header=False),
    PushText("silence.auto_off", "{sender}", "note", "{sender} auto silence off", NoActionNeeded(),
        "You are back; {kept} kept.",
        (PushExample("typical", {"sender": "natedev", "kept": "3"}),
         PushExample("none_kept", {"sender": "natedev", "kept": "0"})),
        purpose="You came back, and auto silence ended.",
        header=False),
)

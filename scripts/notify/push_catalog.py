"""Script phone texts and named cases reviewed before delivery."""
from gpu_xid import XID_MEANINGS
from push_texts import ActionTemplate, NoActionNeeded, PushExample, PushText, SessionTemplate

# Codex usage destination: https://learn.chatgpt.com/docs/pricing (read 2026-10-14).
# Real quota case: notify/review.jsonl, 2026-10-13T22:30:22.569264-04:00.
# Other script senders have no live record in the two supplied logs. Their
# typical cases use existing test facts or the Nix thresholds, never invented log citations.


ENTRIES = (
    PushText("nix_heal.failed", "nix-heal", "blocked", "{session} builds will fail: no nix shell",
        ActionTemplate("On {machine}, fix the nix shell in {cwd}, then restart {session}"),
        "The nix shell for {session}'s worktree did not load, so its builds will fail. "
        + "{cause}\n\n"
        + "The session's worktree changes stay on disk. This alert clears when the session has a nix shell.",
        (PushExample("refused", {"session": "handler", "machine": "natedev", "cwd": "/home/natepiano/worktrees/handler",
            "cause": "The automatic restart could not fix it. The .envrc file loaded, but no nix shell started."}),
         PushExample("still_broken", {"session": "handler", "machine": "Mac", "cwd": "/Users/nate/worktrees/handler",
            "cause": "An automatic restart did not fix it."})),
        purpose="A session has no nix shell and automatic recovery failed.",
        session=SessionTemplate("{session}")),
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
    PushText("shutdown.restart_failed", "shutdown", "decision", "Claude coding work is still stopped",
        ActionTemplate('In the Claude session named natedev on the natedev computer, run /shutdown restart {account}.'),
        "After your restart request, Claude coding work for account {account} is still stopped on {machine}. "
        + "{stopped} did not restart, so that work stays paused.\n\n"
        + "The restart gave this error: {cause}\n\n"
        + "The command above retries the restart on natedev and the Mac.",
        (PushExample("typical", {"account": 'claude2', "machine": "natedev", "stopped": "One session (organon-unit)",
             "cause": "window failed to open"}),
         PushExample("one_long_name", {"account": '"claude-team-build-followups-12"', "machine": "Mac",
             "stopped": "One session (build-followups-two-accounts-unit-director)",
             "cause": "restart prompt not submitted"})),
        purpose="A restart did not start all stopped sessions again."),
    # A retry of an untrusted folder fails the same way, so this push never asks for one first.
    # The command is accounts.trust_command for the folder and the session's account.
    PushText("shutdown.restart_untrusted", "shutdown", "decision", "Stopped Claude work needs your folder trust",
        ActionTemplate("In a terminal on {machine}, run the command below, trust the folder and type /exit. "
                       + "Then run /shutdown restart {account} in the natedev session."),
        "The Claude session {session} did not restart. No Claude account on {machine} trusts its folder. "
        + "The command opens Claude there. Until you trust it, each restart fails.\n\n{command}",
        (PushExample("typical", {"session": "organon-unit", "machine": "natedev", "account": "claude2",
             "command": "cd /home/natepiano/worktrees/organon && CLAUDE_CONFIG_DIR=/home/natepiano/.claude2 "
                        + "CLAUDE_CODE_REMOTE_MEMORY_DIR=/home/natepiano/.claude claude"}),
         PushExample("long_folder", {"session": "build-followups-shutdown", "machine": "Mac", "account": "claude4",
             "command": "cd /Users/natepiano/worktrees/claude-build-followups-shutdown && "
                        + "CLAUDE_CONFIG_DIR=/Users/natepiano/.claude4 CLAUDE_CODE_REMOTE_MEMORY_DIR=/Users/natepiano/.claude claude"})),
        purpose="A restart failed because no account trusts the session's folder."),
    # The move form of shutdown.restart_untrusted: the record lives under the source account, and its
    # restart resumes the move on the target, whose trust the command accepts.
    PushText("shutdown.move_untrusted", "shutdown", "decision", "Stopped Claude work needs your folder trust",
        ActionTemplate("In a terminal on {machine}, run the command below, trust the folder and type /exit. "
                       + "Then run /shutdown restart {account} in the natedev session. That finishes the move to {target}."),
        "The Claude session {session} did not start on {target} after its move from {account}. "
        + "No Claude account on {machine} trusts its folder, so each restart fails until you do.\n\n{command}",
        (PushExample("typical", {"session": "organon-unit", "machine": "natedev", "account": "claude2", "target": "claude3",
             "command": "cd /home/natepiano/worktrees/organon && CLAUDE_CONFIG_DIR=/home/natepiano/.claude3 "
                        + "CLAUDE_CODE_REMOTE_MEMORY_DIR=/home/natepiano/.claude claude"}),
         PushExample("mac", {"session": "ups", "machine": "Mac", "account": "claude1", "target": "claude4",
             "command": "cd /Users/natepiano/worktrees/claude-ups && "
                        + "CLAUDE_CONFIG_DIR=/Users/natepiano/.claude4 CLAUDE_CODE_REMOTE_MEMORY_DIR=/Users/natepiano/.claude claude"})),
        purpose="A move failed because no account trusts the session's folder."),
    PushText("shutdown.move_failed", "shutdown", "decision", "A Claude session did not finish its move",
        ActionTemplate('In the Claude session named natedev on the natedev computer, send: "Finish moving the Claude session named {session} to {target} from its shutdown record."'),
        "The Claude session {session} on {machine} stopped on account {source} to move to account {target}. "
        + "It did not start on {target}, so its work stays paused. "
        + "Its shutdown record starts it on {target}, not on {source}.",
        (PushExample("typical", {"session": "ups", "machine": "natedev", "source": "claude1", "target": "claude3"}),
         PushExample("long_session", {"session": "build-followups-message-clarity-unit-director-session-12",
             "machine": "Mac", "source": "claude2", "target": "claude4"})),
        purpose="A session stopped for an account move did not start on its new account."),
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
        (PushExample('codex_one', {'account': 'codex1', 'reset': 'Tue 2026-10-20 00:58 AST', 'target': 'codex2', 'funding': '97% weekly usage left', 'transition': 'Codex work now uses codex2'}),
         PushExample('codex_two', {'account': 'codex2', 'reset': 'Thu 2026-10-22 07:00 AST', 'target': 'codex1', 'funding': 'its own paid credits', 'transition': 'Codex work now spends paid credits'})),
        purpose="A Codex account ran out, and its work moved to another account."),
    PushText('quota.codex_workers_stay', "quota_alert", "note", '{account} {outcome}',
        NoActionNeeded(),
        'Codex account {account} has exhausted its included weekly usage until {reset}. Credits are off. {reason} {fallback}',
        (PushExample('no_room', {'account': 'codex1', 'reset': 'Tue 2026-10-20 00:58 AST', 'reason': 'No other Codex account has room.', 'outcome': 'work moves to Claude', 'fallback': 'New coding work uses Claude, the other coding assistant, until the earliest Codex refill at Sat 2026-10-17 07:00 AST. Current turns finish first.'}),
         PushExample('failed_move', {'account': 'codex2', 'reset': 'Thu 2026-10-22 07:00 AST', 'reason': 'Workers could not move to codex1 because its usage reading is unavailable.', 'outcome': 'work moves to Claude', 'fallback': 'New coding work uses Claude, the other coding assistant, until the earliest Codex refill at Tue 2026-10-20 00:58 AST. Current turns finish first.'})),
        purpose="A Codex account ran out, and its workers wait or move to Claude."),
    PushText('quota.codex_credits_forecast', "quota_alert", "note", '{account}: paid credits {horizon}',
        NoActionNeeded(),
        '{forecast} {destination}',
        (PushExample('codex_one', {'account': 'codex1', 'forecast': 'Workers on Codex account codex1 used 120 paid credits in the last hour; its 6,370 credits last until about Fri 2026-10-16 13:05 AST.', 'destination': 'When credits run out, workers move to codex2 (97% weekly usage left).', 'horizon': 'until Fri 13:05 AST'}),
         PushExample('codex_two', {'account': 'codex2', 'forecast': 'Workers on Codex account codex2 used 40 paid credits in the last hour; its 2,114 credits last until about Fri 2026-10-16 12:51 AST.', 'destination': 'When credits run out, workers move to codex1 and spend its own paid credits.', 'horizon': 'until Fri 12:51 AST'}),
         PushExample('zero_use', {'account': 'codex1', 'forecast': 'Workers on Codex account codex1 used no credits in the last hour; 6,370 paid credits left.', 'destination': 'When credits run out, workers move to codex2 (97% weekly usage left).', 'horizon': 'use is zero'}),
         PushExample('unlimited', {'account': 'codex1', 'forecast': 'Codex account codex1 has unlimited paid credits; its coding workers remain on paid usage with no credit limit.', 'destination': '', 'horizon': 'have no limit'}),
         PushExample('unknown_balance', {'account': 'codex1', 'forecast': 'Codex account codex1 reports no credit balance; its coding workers remain on paid usage, but the run-out time is unknown.', 'destination': 'When credits run out, workers move to codex2 (97% weekly usage left).', 'horizon': 'run-out unknown'}),
         PushExample('unknown_use', {'account': 'codex2', 'forecast': 'Coding workers on Codex account codex2 remain on paid usage. Their hourly use is unknown because credit observations cover less than 30 minutes of the hour; 2,114 credits left.', 'destination': 'When they run out, Codex work moves to Claude, the other coding assistant, until Tue 2026-10-20 00:58 AST.', 'horizon': 'run-out unknown'}),
         PushExample('queued', {'account': 'codex1', 'forecast': 'Workers on Codex account codex1 used 120 paid credits in the last hour; its 6,370 credits last until about Fri 2026-10-16 13:05 AST.', 'destination': 'When credits run out, workers wait for the account move you queued.', 'horizon': 'until Fri 13:05 AST'})),
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
    # GPU watch examples use the Xid codes in the user's 2026-10-30 alert request.
    PushText("nixos.gpu_stopped", "gpu", "decision", "natedev GPU stopped",
        ActionTemplate("Restart natedev to bring the GPU back."),
        "The GPU stopped at {time}. Both displays are dark. "
        + "The cause is a known bug in the NVIDIA firmware. "
        + "The kernel reported Xid {xid}: {meaning}.",
        (PushExample("xid_79_night", {"xid": "79", "meaning": XID_MEANINGS["79"], "time": "02:17 EDT"}),
         PushExample("xid_119", {"xid": "119", "meaning": XID_MEANINGS["119"], "time": "14:05 EDT"})),
        purpose="natedev's GPU stopped, and both displays are dark."),
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
    # /nate adhoc: an agent writes the values at send time; review them like free text (user, 2026-10-28).
    # The user asked that the template name close each push; pushover.py's "Template:" line does that
    # for every push (user, 2026-10-27).
    PushText("nate.adhoc", "{sender}", "note", "{sender} {title}", NoActionNeeded(),
        "{description}",
        (PushExample("typical", {"sender": "natedev", "title": "the /nate skill is ready",
                                 "description": "You asked to be told when the /nate skill is ready. It is ready now: "
                                 + "/nate adhoc <instructions> sends you an update like this one."}),
         PushExample("long_sender", {"sender": "cargo-liner-style", "title": "renames are done",
                                     "description": "You asked to be told when the tile and handler renames finish. "
                                     + "Both are done, and both units run under their new names."})),
        purpose="An update that an agent writes for you with /nate adhoc.",
        header=False, agent_written=True),
    # Auto-mode refusal (user approved 2026-10-21): a session sends it with send.py --text-id auto_mode.refused.
    # send.py fills sender from the session's own name, action from the commands' first words (rca.action_of),
    # refused from the command values (one per line), waits_line from waits when the 30 s push review passes it
    # (else ""), noun and this from the number of commands, and rule_line from rule.
    PushText("auto_mode.refused", "{sender}", "decision", "{sender}: auto mode stopped {session}",
        ActionTemplate("On {machine}, in the {session} pane, approve the prompt or add an allow rule"),
        "Auto mode refused {action} in the {session} session.{waits_line}\n{refused}\n\n"
        + "Approve: the work goes on now, and auto mode can stop it on {this} {noun} again.\n\n"
        + "Allow rule: the work goes on now, and auto mode lets {this} {noun} run from then on.{rule_line}",
        (PushExample("typical", {"sender": "cargo-liner-style", "session": "modules", "machine": "natedev",
            "action": "2 shell commands", "refused": "implement.sh --to modules-impl\nreview.sh --to modules-test",
            "noun": "commands", "this": "these", "waits_line": " The modules build waits for them.", "rule_line": ""}),
         PushExample("one_with_rule", {"sender": "build-followups", "session": "messaging", "machine": "natedev",
            "action": "a shell command", "refused": "git push origin build-followups-messaging", "noun": "command",
            "this": "this", "waits_line": " The merge of the messaging work waits for it.", "rule_line": " The rule: Bash(git push:*)"}),
         PushExample("file_change_no_waits", {"sender": "hana", "session": "demo", "machine": "natedev",
            "action": "a file change", "refused": "write phases/34h/delegated_phase_reservation_state.json",
            "noun": "command", "this": "this", "waits_line": "", "rule_line": ""})),
        purpose="Auto mode refused a session's commands, and the session waits for you.",
        session=SessionTemplate("{session}")),
    # Permission root cause (user ask, 2026-10-23): rca.py sends these when the owner of a case starts it
    # (`rca.py start`) and when its fix merges (`rca.py fixed`), with --key rca-<case id>. sender is the
    # session's production, or messaging when it has none. rca.py fills time (silence.shown) and stop (what stopped
    # it and the kind of action) from the case, and drops a final period from cause and fix. Only the started push
    # has waits_line, the alert's present-tense waits sentence: once the stop is fixed, nothing waits.
    # No owner, file, command or commit (user, 2026-10-23 19:20). Examples: the demo and handler cases of 2026-10-23.
    PushText("permission.rca_started", "{sender}", "note", "{sender}: finding why {session} stopped",
        NoActionNeeded(),
        "The {session} session stopped at {time} when {stop}.{waits_line} "
        + "A session now finds the cause and fixes it, so it does not stop there again. "
        + "A second push comes when the fix is in.",
        (PushExample("typical", {"sender": "hana", "session": "demo", "time": "14:23 AST",
            "stop": "auto mode refused a file change", "waits_line": " The demo build waits for it."}),
         PushExample("prompt", {"sender": "cargo-liner", "session": "handler", "time": "09:01 AST",
            "stop": "it asked you to allow a shell command", "waits_line": ""})),
        purpose="Someone starts to find why a session stopped for a permission."),
    PushText("permission.rca_fixed", "{sender}", "note", "{sender}: fixed why {session} stopped",
        NoActionNeeded(),
        "The {session} session stopped at {time} when {stop}.\nWhy: {cause}.\nNow: {fix}.",
        (PushExample("typical", {"sender": "hana", "session": "demo", "time": "14:23 AST",
            "stop": "auto mode refused a file change",
            "cause": "the steps told the session to write a progress file by hand, which auto mode does not allow",
            "fix": "a script writes that file, so the session never has to"}),
         PushExample("prompt", {"sender": "cargo-liner", "session": "handler", "time": "09:01 AST",
            "stop": "it asked you to allow a shell command",
            "cause": "a helper ran a build command that needs your permission",
            "fix": "helpers use the build command that is always allowed"})),
        purpose="The fix merged: the session does not stop for that permission again."),
    # rca.py fixed sends this when the push review refused the fixed text twice: the reviewer's own rewrite.
    PushText("permission.rca_fixed_rewritten", "{sender}", "note", "{title}", NoActionNeeded(), "{body}",
        (PushExample("typical", {"sender": "hana", "title": "hana: fixed why demo stopped",
            "body": "The demo session stopped at 14:23 AST when auto mode refused a file change.\n"
            + "Why: its steps told it to write a progress file by hand.\nNow: a script writes that file."}),),
        purpose="The fix merged; the push review wrote this text after two refusals."),
    # Paid credits (user approved 2026-10-21): sent with send.py --text-id quota.paid_credits. send.py fills
    # sender from the session's own name; a script with no session record names itself with --from.
    # Examples: the codex2 values the user approved, and codex1 sent from the natedev session.
    PushText("quota.paid_credits", "{sender}", "decision", "{sender}: {account} now uses paid credits",
        ActionTemplate("To stop it, set use_credits: off in ~/rust/hanadocs/agents/{account}.md on {machine}"),
        "{account} has {credits} paid credits left. Its plan refills {refill}.\n\n"
        + "Leave on (recommended): work spends these credits, then moves to Claude.\n\n"
        + "Turn off: work moves to Claude until {next_reset}.",
        (PushExample("typical", {"sender": "quota_alert", "account": "codex2", "machine": "natedev",
            "credits": "25,345", "refill": "Tue 10-27 02:33 AST", "next_reset": "Sat 10-24 16:40 AST"}),
         PushExample("from_session", {"sender": "natedev", "account": "codex1", "machine": "natedev",
            "credits": "6,370", "refill": "Thu 10-29 07:00 AST", "next_reset": "Sat 10-24 16:40 AST"})),
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
    PushText("silence.auto_off", "{sender}", "note", "{sender}: you are back", NoActionNeeded(),
        "{held}",
        (PushExample("typical", {"sender": "natedev", "held": "The 3 pushes held while you were away come next."}),
         PushExample("one_held", {"sender": "natedev", "held": "The push held while you were away comes next."}),
         PushExample("none_held", {"sender": "natedev", "held": "No pushes were held while you were away."})),
        purpose="You came back, and auto silence ended.",
        header=False),
)

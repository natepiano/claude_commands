"""Script phone texts and named cases reviewed before delivery."""
from push_texts import ActionTemplate, NoActionNeeded, PushExample, PushText, SessionTemplate

# Codex usage destination: https://learn.chatgpt.com/docs/pricing (read 2026-10-14).
# Real quota case: notify/review.jsonl, 2026-10-13T22:30:22.569264-04:00.
# Other script senders have no live record in the two supplied logs. Their
# typical cases use existing test facts or the Nix thresholds, never invented log citations.


ENTRIES = (
    PushText("shutdown.holdout", "shutdown", "decision", "{session} {operation} is blocked",
        ActionTemplate("answer the open form, or run the command below to stop {session}"),
        "Reason: {operation} is blocked because {session}'s claude session is not answering, and it can't proceed.\n\n"
        + "Stop now (recommended): ends {session} mid-step so the {request} goes on. Uncommitted work stays; {after_stop}\n\n"
        + "Do nothing: the {request} keeps waiting. It has waited {elapsed} so far.\n\nCommand: {command} (on {machine})",
        (PushExample("typical", {"session": "Owner", "machine": "natedev",
            "elapsed": "4 minutes", "operation": "Restart", "request": "restart", "after_stop": "the restart brings it back.",
            "command": "~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py now 'claude 2' --here --only owner"}),),
        session=SessionTemplate("{session}")),
    # Shutdown examples use test_settle.py and test_restart.py facts.
    # The failing shutdown deliveries are recorded in notify/review.jsonl, 2026-10-14T02:05Z.
    PushText("shutdown.stop_failed", "shutdown", "decision", "Claude account shutdown is incomplete",
        ActionTemplate('In the Claude session named natedev on the natedev computer, run /shutdown now {account}.'),
        "The shutdown of Claude account {account} did not finish on {machine}. "
        + "{stopped} did not stop, so work may still be running there. The command above retries the cleanup.",
        (PushExample("typical", {"account": 'claude2', "machine": "natedev", "stopped": "One session"}),
         PushExample("many_long_names", {"account": '"claude-team-build-followups-12"', "machine": "Mac", "stopped": "14 sessions"}))),
    PushText("shutdown.restart_failed", "shutdown", "decision", "Claude coding work is still stopped",
        ActionTemplate('In the Claude session named natedev on the natedev computer, run /shutdown restart {account}.'),
        "After your restart request, Claude coding work for account {account} is still stopped on {machine}. "
        + "{stopped} did not restart, so that work stays paused. "
        + "The command above retries the restart on natedev and the Mac.",
        (PushExample("typical", {"account": 'claude2', "machine": "natedev", "stopped": "One session"}),
         PushExample("many_long_names", {"account": '"claude-team-build-followups-12"', "machine": "Mac", "stopped": "14 sessions"}))),
    PushText("shutdown.restart_incomplete", "shutdown", "decision", "Claude restart could not be confirmed",
        ActionTemplate('In the Claude session named natedev on the natedev computer, run /shutdown restart {account}.'),
        "The restart of Claude account {account} could not be confirmed on {machine}. "
        + "Its stopped sessions may still be unable to work. Retrying asks any sessions that are still stopped to start again.",
        (PushExample("typical", {"account": 'claude2', "machine": "Mac"}),
         PushExample("long_account", {"account": '"claude-team-build-followups-12"', "machine": "natedev"}))),
    PushText("shutdown.manual_restart", "shutdown", "decision", "Stopped Claude coding work needs you",
        ActionTemplate('In the Claude session named natedev on the natedev computer, send: "Resume the stopped Claude session named {session} on {machine} from its shutdown record."'),
        "A Claude coding session on {machine} stopped during your shutdown. Its work is paused. "
        + "Its terminal cannot restart on its own. Its shutdown record holds the command to resume it.",
        (PushExample("typical", {"machine": "natedev", "session": "Owner"}),
         PushExample("long_session", {"machine": "Mac", "session": "build-followups-message-clarity-unit-director-session-12"}))),
    PushText("shutdown.remote_control", "shutdown", "decision", "Claude session lacks phone access",
        ActionTemplate("In the terminal for Claude session {session} on {machine}, run /remote-control."),
        "Claude session {session} on {machine} restarted and is running. Remote Control is off, "
        + "so you cannot reach it from your phone until you turn it on.",
        (PushExample("typical", {"machine": "natedev", "session": "Owner"}),
         PushExample("long_session", {"machine": "Mac", "session": "build-followups-message-clarity-unit-director-session-12"}))),
    PushText("quota.claude", "quota_alert", "decision", "Coding may stop: Claude account out of usage",
        ActionTemplate("In a Claude terminal on natedev, run /login and sign in to account {target}."),
        "Claude account {account} has {remaining} of its weekly usage left. Its coding work may stop until {reset}. "
        + "Account {target} still has {target_remaining} of its weekly usage. If you switch to it, the work can continue.",
        (PushExample("typical", {"account": "claude3", "remaining": "2%", "reset": "2026-10-17 09:00 EDT", "target": "claude2", "target_remaining": "60%"}),
         PushExample("exhausted", {"account": "claude999", "remaining": "0%", "reset": "2026-10-19 23:59 EDT", "target": "claude2", "target_remaining": "60%"}),
         PushExample("long_target", {"account": "claude3", "remaining": "0%", "reset": "2026-10-19 23:59 EDT", "target": "claude-team-build-followups-12", "target_remaining": "40%"}))),
    PushText('quota.codex_on_credits', "quota_alert", "note", 'Codex workers now spend paid credits',
        ActionTemplate('{credit_action}, set use_credits: off in ~/rust/hanadocs/agents/{file} on {machine}.'),
        '{opening}\n\n{turn_off}\n\n{leave_on}{clarification}',
        (PushExample('codex_one', {'file': 'codex1.md', 'machine': 'natedev', 'turn_off': 'Turn off (recommended): no paid spend. Within minutes, new work moves to the other account, codex2 (97% of its weekly allowance left).', 'leave_on': 'Leave on: codex1 spends its credits, then work moves to codex2.', 'credit_action': 'To stop paid credit use', 'opening': 'Codex account codex1 is now spending paid credits until its weekly refill on Tue 2026-10-20 00:58 EDT. Its 6,370 credits last until about Fri 2026-10-16 13:05 EDT.', 'clarification': ''}),
         PushExample('codex_two', {'file': 'codex2.md', 'machine': 'natedev', 'turn_off': "Turn off: within minutes, new work spends codex1's paid credits instead.", 'leave_on': "Leave on (recommended, no edit needed): new work spends codex2's credits, then codex1's, then falls back to Claude.", 'credit_action': "Optional: to spend codex1's credits first", 'opening': 'Codex account codex2 now spends paid credits until its included usage refills Thu 2026-10-22 07:00 EDT. Its 2,114 credits last until about Fri 2026-10-16 12:51 EDT.', 'clarification': ''}),
         PushExample('unlimited', {'file': 'codex1.md', 'machine': 'natedev', 'turn_off': 'Turn off (recommended): no paid spend. Within minutes, new work moves to the other account, codex2 (97% of its weekly allowance left).', 'leave_on': "Leave on: keep spending codex1's paid credits without a limit.", 'credit_action': 'To stop paid credit use', 'opening': 'Codex account codex1 is now spending paid credits until its weekly refill on Tue 2026-10-20 00:58 EDT. There is no limit on paid credits.', 'clarification': ''}),
         PushExample('unknown', {'file': 'codex1.md', 'machine': 'natedev', 'turn_off': 'Turn off (recommended): paid spending stops. Within minutes, new work moves to the second account, codex2, which has 97% of its weekly limit left.', 'leave_on': 'Leave on: codex1 spends its credits, then new work moves to codex2.', 'credit_action': 'To stop paid credit use', 'opening': 'Codex account codex1 is now spending paid credits until its weekly refill on Tue 2026-10-20 00:58 EDT. The credit balance is unknown.', 'clarification': ''}))),
    PushText('quota.codex_on_credits_last', "quota_alert", "note", 'Codex workers now spend paid credits',
        ActionTemplate('To stop paid credit use, set use_credits: off in ~/rust/hanadocs/agents/{file} on {machine}.'),
        'Codex account {account} now uses paid credits. It refills {reset}. {credits}\n{turn_off}\n{leave_on}',
        (PushExample('codex_one', {'account': 'codex1', 'file': 'codex1.md', 'machine': 'natedev', 'reset': 'Tue 2026-10-20 00:58 EDT', 'credits': 'Its 6,370 credits last until about Fri 2026-10-16 13:05 EDT.', 'leave_on': 'Leave on: spend these credits, then use Claude when they run out.', 'turn_off': 'Turn off (recommended, saves paid credits): within minutes, work uses Claude until Sat 2026-10-17 07:00 EDT, the next Codex reset, then returns to Codex if an account has room.'}),
         PushExample('codex_two', {'account': 'codex2', 'file': 'codex2.md', 'machine': 'natedev', 'reset': 'Thu 2026-10-22 07:00 EDT', 'credits': 'Its 2,114 credits last until about Fri 2026-10-16 12:51 EDT.', 'leave_on': 'Leave on: spend these credits, then use Claude when they run out.', 'turn_off': 'Turn off (recommended, saves paid credits): within minutes, work uses Claude until Tue 2026-10-20 00:58 EDT, the next Codex reset, then returns to Codex if an account has room.'}))),
    PushText('quota.codex_workers_moved', "quota_alert", "note", '{transition}',
        NoActionNeeded(),
        'Codex account {account} has exhausted its included weekly usage until {reset}. Credits are off, so new work moves to the other Codex account, {target}, using {funding}. Jobs already running finish their current turn first.',
        (PushExample('codex_one', {'account': 'codex1', 'reset': 'Tue 2026-10-20 00:58 EDT', 'target': 'codex2', 'funding': '97% weekly usage left', 'transition': 'Codex work now uses codex2'}),
         PushExample('codex_two', {'account': 'codex2', 'reset': 'Thu 2026-10-22 07:00 EDT', 'target': 'codex1', 'funding': 'its own paid credits', 'transition': 'Codex work now spends paid credits'}))),
    PushText('quota.codex_workers_stay', "quota_alert", "note", '{account} {outcome}',
        NoActionNeeded(),
        'Codex account {account} has exhausted its included weekly usage until {reset}. Credits are off. {reason} {fallback}',
        (PushExample('no_room', {'account': 'codex1', 'reset': 'Tue 2026-10-20 00:58 EDT', 'reason': 'No other Codex account has room.', 'outcome': 'work moves to Claude', 'fallback': 'New coding work uses Claude, the other coding assistant, until the earliest Codex refill at Sat 2026-10-17 07:00 EDT. Current turns finish first.'}),
         PushExample('failed_move', {'account': 'codex2', 'reset': 'Thu 2026-10-22 07:00 EDT', 'reason': 'Workers could not move to codex1 because its usage reading is unavailable.', 'outcome': 'work moves to Claude', 'fallback': 'New coding work uses Claude, the other coding assistant, until the earliest Codex refill at Tue 2026-10-20 00:58 EDT. Current turns finish first.'}))),
    PushText('quota.codex_credits_forecast', "quota_alert", "note", '{account}: paid credits {horizon}',
        NoActionNeeded(),
        '{forecast} {destination}',
        (PushExample('codex_one', {'account': 'codex1', 'forecast': 'Workers on Codex account codex1 used 120 paid credits in the last hour; its 6,370 credits last until about Fri 2026-10-16 13:05 EDT.', 'destination': 'When credits run out, workers move to codex2 (97% weekly usage left).', 'horizon': 'until Fri 13:05 EDT'}),
         PushExample('codex_two', {'account': 'codex2', 'forecast': 'Workers on Codex account codex2 used 40 paid credits in the last hour; its 2,114 credits last until about Fri 2026-10-16 12:51 EDT.', 'destination': 'When credits run out, workers move to codex1 and spend its own paid credits.', 'horizon': 'until Fri 12:51 EDT'}),
         PushExample('zero_use', {'account': 'codex1', 'forecast': 'Workers on Codex account codex1 used no credits in the last hour; 6,370 paid credits left.', 'destination': 'When credits run out, workers move to codex2 (97% weekly usage left).', 'horizon': 'use is zero'}),
         PushExample('unlimited', {'account': 'codex1', 'forecast': 'Codex account codex1 has unlimited paid credits; its coding workers remain on paid usage with no credit limit.', 'destination': '', 'horizon': 'have no limit'}),
         PushExample('unknown_balance', {'account': 'codex1', 'forecast': 'Codex account codex1 reports no credit balance; its coding workers remain on paid usage, but the run-out time is unknown.', 'destination': 'When credits run out, workers move to codex2 (97% weekly usage left).', 'horizon': 'run-out unknown'}),
         PushExample('unknown_use', {'account': 'codex2', 'forecast': 'Coding workers on Codex account codex2 remain on paid usage. Their hourly use is unknown because credit observations cover less than 30 minutes of the hour; 2,114 credits left.', 'destination': 'When they run out, Codex work moves to Claude, the other coding assistant, until Tue 2026-10-20 00:58 EDT.', 'horizon': 'run-out unknown'}))),
    PushText("quota.codex_refused", "quota_alert", "decision", "Codex work stopped for lack of usage",
        ActionTemplate("At chatgpt.com/codex/settings/usage, add credits for account {account}."),
        "Codex hit its usage limit on account {account} and stopped accepting coding jobs. "
        + "Moving those jobs to Claude, the other coding assistant, also failed, so the work is paused. "
        + "Adding credits lets Codex accept jobs again.",
        (PushExample("typical", {"account": "codex1"}),
         PushExample("long_account", {"account": "codex999"}))),
    PushText("sweep.disk_floor", "sweep", "note", "Builds need more disk space",
        ActionTemplate("In a terminal on natedev, delete {shortfall} of personal files you no longer need."),
        "The natedev computer has {free} free; builds need {floor}. Build cache cleanup freed {removed}. "
        + "Builds may fail until you free more space.",
        (PushExample("typical", {"shortfall": "10.0 GiB", "free": "290.0 GiB", "floor": "300.0 GiB", "removed": "2.0 GiB"}),
         PushExample("no_free_space", {"shortfall": "300.0 GiB", "free": "0.0 GiB", "floor": "300.0 GiB", "removed": "0.0 GiB"}))),
    PushText("rust.passed", "rust_release", "decision", "Hana can use the new Rust compiler",
        ActionTemplate("In the Claude session named natedev, reply upgrade or wait."),
        "Hana, the editor project, passed its compiler check on Rust {version}: no errors. "
        + "It currently uses Rust {pin}.\n\n"
        + "Upgrade (recommended because the check passed): use Rust {version} for Hana's builds.\n\n"
        + "Wait: keep building Hana with Rust {pin}.",
        (PushExample("typical", {"version": "1.100.0", "pin": "1.99.0"}),
         PushExample("long_versions", {"version": "100.100.100", "pin": "99.99.99"}))),
    PushText("rust.warnings", "rust_release", "decision", "New compiler adds warnings to Hana",
        ActionTemplate("In the Claude session named natedev, reply wait or upgrade."),
        "Hana, the editor project, passed its code check on Rust {version}, but gained {warnings} warnings. "
        + "Warnings do not prevent builds. It still uses Rust {pin}.\n\n"
        + "Wait (recommended to avoid new warnings): keep Rust {pin}.\n\n"
        + "Upgrade: use Rust {version} with those warnings.",
        (PushExample("typical", {"version": "1.100.0", "pin": "1.99.0", "warnings": "2"}),
         PushExample("many_warnings", {"version": "1.100.0", "pin": "1.99.0", "warnings": "14"}))),
    PushText("rust.errors", "rust_release", "decision", "Rust upgrade failed a code check",
        ActionTemplate("In the Claude session named natedev, reply wait or upgrade."),
        "Rust {version} failed its code check on Hana, the editor project. Errors: {errors}; new warnings: {warnings}. "
        + "Hana still uses Rust {pin}.\n\n"
        + "Wait (recommended to keep builds working): keep Rust {pin}.\n\n"
        + "Upgrade: use Rust {version} despite the errors; Hana's builds may fail.",
        (PushExample("typical", {"version": "1.100.0", "pin": "1.99.0", "errors": "1", "warnings": "2"}),
         PushExample("many_errors", {"version": "1.100.0", "pin": "1.99.0", "errors": "14", "warnings": "14"}))),
    PushText("rust.repair_failed", "rust_release", "decision", "Rust upgrade breaks the build repair tool",
        ActionTemplate("In the Claude session named natedev, reply wait or upgrade."),
        "Rust {version} is out. The tool that repairs Rust build errors fails to compile with it. Hana, the editor project, uses Rust {pin}.\n\n"
        + "Wait (recommended): Hana stays on {pin} and keeps build repair. No fix is scheduled.\n\n"
        + "Upgrade: Hana moves to {version}. It may lose build repair. No benefit for Hana is confirmed.",
        (PushExample("typical", {"version": "1.100.0", "pin": "1.99.0"}),
         PushExample("long_versions", {"version": "100.100.100", "pin": "99.99.99"}))),
    PushText("codex.reset_ready", "codex-winddown", "decision", "Codex is ready for your reset",
        ActionTemplate("At chatgpt.com/codex/settings/usage, redeem your limit reset."),
        "All Codex coding agents on {machine}, your computer, have stopped. Redeeming your usage reset now will not interrupt any work. "
        + "New Codex work stays paused until you give the all clear: in a Claude session on {machine}, run /codex_winddown clear.",
        (PushExample("typical", {"machine": "natedev"}),
         PushExample("long_machine", {"machine": "macbook-workstation"}))),
    # Current disk-floor.nix: alertGib=150, build floor=300 GiB.
    PushText("nixos.disk_low", "disk_floor", "note", "Builds need more disk space",
        ActionTemplate("In a terminal on natedev, delete {shortfall} GiB of personal files you no longer need."),
        "The natedev computer has {free} GiB free on its main disk. Builds need 300 GiB. "
        + "Automated cleanup could not restore that space; builds may fail until you free more.",
        (PushExample("typical", {"free": "149", "shortfall": "151"}),
         PushExample("no_free_space", {"free": "0", "shortfall": "300"}))),
    # Current ups.nix: low-runtime shutdown at five minutes or 30% charge.
    PushText("nixos.ups_battery", "ups", "blocked", "Power outage threatens open work",
        ActionTemplate("Save your open work on the natedev computer now."),
        "Mains power is out. The natedev computer is on backup battery and could shut down soon. "
        + "Charge: {charge}%. Time left: about {minutes} minutes. It shuts down at 30% charge. "
        + "It also shuts down at 5 minutes left. This prevents the battery dying while a file is being saved.",
        (PushExample("typical", {"charge": "80", "minutes": "20"}),
         PushExample("shutdown_threshold", {"charge": "30", "minutes": "5"}))),
    PushText("nixos.ups_shutdown", "ups", "blocked", "Power outage stopped the computer",
        ActionTemplate("When power returns, start natedev if it stays off for three minutes."),
        "The natedev computer is shutting down because its backup battery is low. "
        + "Work cannot resume until it starts again. It should start automatically when mains power returns.",
        (PushExample("typical", {}),)),
)

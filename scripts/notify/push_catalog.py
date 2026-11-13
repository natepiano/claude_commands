"""Script phone texts and named cases reviewed before delivery."""
from scripts.notify.gpu_xid import XID_MEANINGS
from scripts.notify.push_texts import ActionTemplate, NoActionNeeded, PushExample, PushText, SessionTemplate, decision_values

# Codex usage destination: https://learn.chatgpt.com/docs/pricing (read 2026-10-14).
# Real quota case: notify/review.jsonl, 2026-10-13T22:30:22.569264-04:00.
# Other script senders have no live record in the two supplied logs. Their
# typical cases use existing test facts or the Nix thresholds, never invented log citations.


def mac_idle_reservation(holder: str, detail: str) -> str:
    """The optional reservation sentence used as a catalog value."""
    return (f'The Mac reservation lists holder "{holder}" and label "{detail}".'
            if holder not in ("", "none") else "")


ENTRIES = (
    # These examples use regional permission errors.
    PushText("aws.snapshot_failed", "buildlog", "note", "AWS prices cannot refresh",
        ActionTemplate("On natedev, run deploy.py up --workload merge-gates --approve-resources in a terminal."),
        "The /aws_prices command cannot refresh AWS prices or quotas.\n{failures}",
        (PushExample("permission", {
            "failures": "AWS denied the price read in us-east-2 (UnauthorizedOperation)."}),
         PushExample("regional_permissions", {
            "failures": "AWS denied the price read in us-east-2 (UnauthorizedOperation).\nAWS denied the Standard quota read in us-west-2 (AccessDenied)."})),
        purpose="AWS denied a regional read that the gate deployment can repair."),
    PushText("nightly.assigned", "sdlc", "note", "Nightly check {outcome}", NoActionNeeded(),
        "sdlc received it at {received}.\nAssigned to {unit} at {assigned}.\n"
        + "The nightly check of {branch} {outcome} at {time}.\nType check: {types}.\nFull tests: {tests}.",
        (PushExample("tests_failed", {"branch": "main", "time": "03:00 EST", "outcome": "failed",
            "types": "passed", "tests": "failed in production/test_staff.py", "received": "03:00 EST",
            "unit": "staff", "assigned": "03:05 EST"}),
         PushExample("types_failed", {"branch": "main", "time": "03:00 EST", "outcome": "failed",
            "types": "failed with 3 errors, 0 warnings, 0 notes", "tests": "passed", "received": "03:00 EST",
            "unit": "types", "assigned": "03:05 EST"})),
        purpose="sdlc assigned the failed nightly check to a unit director."),
    PushText("nightly.unassigned", "sdlc", "note", "Nightly check {outcome}", NoActionNeeded(),
        "{notice}.\nsdlc has not assigned the fix yet.\n"
        + "The nightly check of {branch} {outcome} at {time}.\nType check: {types}.\nFull tests: {tests}.",
        (PushExample("received", {"branch": "main", "time": "03:00 EST", "outcome": "failed",
            "types": "passed", "tests": "failed in production/test_staff.py",
            "notice": "sdlc received it at 03:00 EST"}),
         PushExample("unreceived", {"branch": "main", "time": "03:00 EST", "outcome": "failed",
            "types": "failed with 3 errors, 0 warnings, 0 notes", "tests": "passed",
            "notice": "sdlc has not confirmed receipt"})),
        purpose="The failed nightly check has no assignment at its deadline."),
    PushText("nightly.result", "nightly", "note", "Nightly check {outcome}", NoActionNeeded(),
        "The nightly check of {branch} finished at {time}.\nType check: {types}.\nFull tests: {tests}.",
        (PushExample("passed", {"branch": "main", "time": "03:53 EST", "outcome": "passed",
                               "types": "passed", "tests": "passed"}),
         PushExample("tests_failed", {"branch": "main", "time": "03:53 EST", "outcome": "failed",
             "types": "passed", "tests": "failed in direct/test_simplify_lens.py, production/test_staff.py"}),
         PushExample("types_failed", {"branch": "main", "time": "03:53 EST", "outcome": "failed",
             "types": "failed with 3 errors, 1 warning, 0 notes", "tests": "passed"}),
         PushExample("many_failed", {"branch": "main", "time": "03:53 EST", "outcome": "failed",
             "types": "passed", "tests": "failed in direct/test_assign.py, direct/test_board.py, "
             + "message/test_send.py, notify/test_review.py, production/test_staff.py, "
             + "shutdown/test_restart.py, settings/test_settings.py, whoami/test_usage.py and 12 more"}),
         PushExample("did_not_finish", {"branch": "main", "time": "03:53 EST", "outcome": "failed",
                                       "types": "did not run", "tests": "did not finish"})),
        purpose="The nightly check finished. It says which parts passed."),
    PushText("nightly.not_run", "nightly", "note", "Nightly check did not run", ActionTemplate("{action}"),
        "The nightly check of {branch} did not run at {time}.\nWhy: {reason}.",
        (PushExample("fetch_failed", {"branch": "main", "time": "03:53 EST",
             "reason": "git fetch of main failed: ssh connection timed out",
             "action": "None. The next nightly run tries again."}),
         PushExample("cold_credentials", {"branch": "main", "time": "03:53 EST",
             "reason": "GitHub credentials are cold",
             "action": "In a terminal on natedev, run github-warmup"})),
        purpose="The nightly check did not run. It says why."),
    PushText("decision.choices", "{sender}", "decision", "{title}",
        ActionTemplate("On {machine}, reply in the {session} terminal: {choice_labels}."),
        "Situation: {situation}\n\nChoices:\n{choices}\n\n"
        + "Recommendation: {recommended_choice}. {recommendation_reason}",
        (PushExample("hana", {
            "sender": "hana", "title": "Assembly opening stalls on cable routes", "session": "hana", "machine": "natedev",
            "situation": "Opening an Assembly stalls for several seconds because each animation frame recalculates all 26 cable routes.",
            "choices": '[{"label":"Fix now","effect":"cables-unit makes this small fix, then returns to its hold. Assemblies open without this stall."},{"label":"Wait","effect":"cables-unit remains on hold. The stall remains until its cable work resumes."}]',
            "recommended_choice": "Fix now",
            "recommendation_reason": "The small fix removes the stall without resuming the rest of the held cable work."}),),
        purpose="A decision with structured choices and a recommendation.", session=SessionTemplate("{session}"),
        agent_written=True, expand_values=decision_values),
    PushText("review_watch.acknowledge", "{session}", "decision", "Review report needs your acknowledgment",
        ActionTemplate("In the {session} terminal on {machine}, acknowledge the review report."),
        "Situation: {report}\n\nChoices:\n"
        + "Acknowledge: You confirm that you read the report. Later build reports stop showing this reminder.\n"
        + "Wait: The reminder remains in later build reports.\n\n"
        + "Recommendation: Acknowledge after you read the report.",
        (PushExample("report", {"session": "natedev", "machine": "natedev", "report": "12 of 12 phases that change the screen merged without the extra code reviewer: report ready, waiting for your acknowledgment"}),),
        purpose="A build report reminds you to acknowledge its review report.", session=SessionTemplate("{session}"), agent_written=True),
    PushText("mac_idle.blocked", "Mac idle watch", "blocked", "{holder} blocks waiting Mac tests",
        ActionTemplate("On natedev, in the {holder} session, ask it to finish its Mac work and run /build:mac unblock"),
        "No natedev build or test has run on the Mac for {minutes} minutes, since {since}. "
        + "{waiting} steps wait behind the {holder} session's block. It labeled the reservation \"{detail}\". "
        + "Waiting work can use the Mac after {clears}.",
        (PushExample("research_block", {"minutes": "10", "waiting": "7", "holder": "research",
            "detail": "P15 F11 L0 run", "since": "07:47 EDT", "clears": "/build:mac unblock by research"}),
         PushExample("three_waiting", {"minutes": "12", "waiting": "3", "holder": "research",
            "detail": "P15 F11 L0 run", "since": "07:47 EDT", "clears": "/build:mac unblock by research"})),
        purpose="A Mac block holds up eligible steps past the configured threshold."),
    PushText("mac_idle.stuck", "Mac idle watch", "decision", "Waiting tests are not using the Mac",
        ActionTemplate("On natedev, ask the builds showrunner to get the waiting steps onto the Mac"),
        "No natedev build or test has run on the Mac for {minutes} minutes, since {since}. "
        + "{waiting} steps wait. {reservation} "
        + "To restore Mac work: {clears}.",
        (PushExample("idle_research_claim", {"minutes": "10", "waiting": "3",
            "reservation": mac_idle_reservation("research", "P15 F11 L0 run"), "since": "07:50 EDT", "clears": "release of the idle claim P15 F11 L0 run"}),
         PushExample("free_mac", {"minutes": "10", "waiting": "7",
            "reservation": mac_idle_reservation("none", ""), "since": "07:47 EDT",
            "clears": "the builds showrunner checks why the waiting steps do not take the Mac"})),
        purpose="Eligible steps wait while the Mac has no build or test."),
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
    PushText("shutdown.restart_failed", "shutdown", "decision", "Claude coding work remains stopped",
        ActionTemplate('In the Claude session named natedev on the natedev computer, run /agents restart {account}.'),
        "After your restart request, {stopped} on Claude account {account} did not restart on {machine}. "
        + "Coding work stays stopped until the restart succeeds.\n\n"
        + "The restart gave this error: {cause}.\n\n"
        + "The command retries only stopped sessions. Running sessions keep their work.",
        (PushExample("typical", {"account": 'claude2', "machine": "natedev", "stopped": "5 sessions",
             "cause": "window failed to open"}),
         PushExample("one_long_name", {"account": 'claude3', "machine": "Mac",
             "stopped": "1 session",
             "cause": "restart prompt not submitted"})),
        purpose="One restart problem sends one push with the failed session count."),
    PushText("shutdown.restart_untrusted", "shutdown", "decision", "Stopped Claude work needs folder trust",
        ActionTemplate("In a terminal on {machine}, run ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py trust. "
                       + "Then run /agents restart {account} in the natedev session."),
        "After your restart request, {stopped} on Claude account {account} could not start on {machine}. Coding work stays stopped. "
        + "The first command trusts the configured folders. The second retries stopped sessions.",
        (PushExample("typical", {"stopped": "3 sessions", "machine": "natedev", "account": "claude2"}),),
        purpose="One push counts sessions that need folder trust on one machine."),
    PushText("shutdown.restart_untrusted_unlisted", "shutdown", "decision", "Stopped Claude work needs folder trust",
        ActionTemplate('In the Claude session named natedev on the natedev computer, send: "Trust the folders of the stopped Claude sessions on {machine} from their shutdown records."'),
        "After your restart request, {stopped} on Claude account {account} could not start on {machine}. "
        + "Claude does not trust the folders. "
        + "Coding work stays stopped until the folders are trusted and the sessions restart.",
        (PushExample("one_session", {"stopped": "1 session", "machine": "Mac", "account": "claude3"}),
         PushExample("several_sessions", {"stopped": "4 sessions", "machine": "natedev", "account": "claude2"})),
        purpose="Stopped sessions need trust for folders outside the configured list."),
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
        ActionTemplate('In the Claude session named natedev on the natedev computer, run /agents restart {account}.'),
        "The restart of Claude account {account} could not be confirmed on {machine}. "
        + "Its stopped sessions may still be unable to work. Retrying asks any sessions that are still stopped to start again.",
        (PushExample("typical", {"account": 'claude2', "machine": "Mac"}),
         PushExample("long_account", {"account": '"claude-team-build-followups-12"', "machine": "natedev"})),
        purpose="A restart ended, but its result could not be confirmed."),
    PushText("shutdown.manual_restart", "shutdown", "decision", "Stopped Claude coding work needs you",
        ActionTemplate('In the Claude session named natedev on the natedev computer, send: "Resume the stopped Claude sessions on {machine} from their shutdown records."'),
        "After your shutdown, {stopped} on {machine} cannot restart without help. "
        + "Coding work stays stopped. The shutdown records hold the commands to resume the stopped sessions.",
        (PushExample("typical", {"machine": "natedev", "stopped": "3 sessions"}),
         PushExample("one_session", {"machine": "Mac", "stopped": "1 session"})),
        purpose="Stopped Claude sessions need their saved commands to resume."),
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
    PushText("sweep.disk_floor", "disk_floor", "decision", "{machine} disk is low; {machine} tests can fail",
        ActionTemplate("{folder_action}"),
        "{machine} has {free} free, below the {floor} alert line, so {machine} test builds can fail. {recommendation}",
        (PushExample("available", {
            "machine": "natedev",
            "folder_action": "In the natedev Claude session, say which natedev folders to delete.",
            "free": "140.0 GiB", "floor": "150.0 GiB",
            "recommendation": "Delete ~/.local/share/Trash (recommended): this frees 26.0 GiB, which covers the 10.0 GiB shortfall."}),
         PushExample("unavailable", {
            "machine": "natedev",
            "folder_action": "In the natedev Claude session, reply measure or delete nothing.",
            "free": "140.0 GiB", "floor": "150.0 GiB",
            "recommendation": "Folder sizes are unknown. Measure (recommended): natedev lists folder sizes, then you choose. Delete nothing: free space stays low."}),
         PushExample("insufficient", {
            "machine": "natedev",
            "folder_action": "In the natedev Claude session, say which natedev folders to delete.",
            "free": "120.0 GiB", "floor": "150.0 GiB",
            "recommendation": "The largest folder is ~/Trash at 20.0 GiB. Deleting it frees only part of the 30.0 GiB shortfall; the natedev session lists every folder."}),
         PushExample("protected", {
            "machine": "Mac",
            "folder_action": "In the natedev Claude session, say which Mac folders to delete.",
            "free": "140.0 GiB", "floor": "150.0 GiB",
            "recommendation": "Review ~/old-output first (recommended): deleting it frees 26.0 GiB, which covers the 10.0 GiB shortfall."})),
        purpose="A low-disk alert recommends one folder for natedev review."),
    PushText("sweep.disk_floor_recovered", "disk_floor", "note", "{machine} disk space is back", NoActionNeeded(),
        "{machine} has {free} free, above the {rearm} line. The next fall below {floor} sends one alert.",
        (PushExample("natedev", {"machine": "natedev", "free": "156.0 GiB", "rearm": "155.0 GiB", "floor": "150.0 GiB"}),
         PushExample("Mac", {"machine": "Mac", "free": "156.0 GiB", "rearm": "155.0 GiB", "floor": "150.0 GiB"})),
        purpose="The disk stayed above the rearm line after a delivered low-space alert."),
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
    PushText("nixos.ups_replace_battery", "ups", "note", "UPS battery needs replacing",
        ActionTemplate("Replace the battery in the natedev UPS."),
        "The natedev UPS reports that its battery is worn out. During a power outage it may not keep natedev on long enough to shut down safely.",
        (PushExample("typical", {}),),
        purpose="The UPS battery is worn out and needs replacement."),
    PushText("nixos.ups_no_contact", "ups", "note", "natedev lost contact with its UPS",
        ActionTemplate("Check the USB cable between the UPS and natedev."),
        "natedev cannot read its UPS. During a power outage it gets no warning and cannot shut down safely.",
        (PushExample("typical", {}),),
        purpose="natedev cannot read the UPS and needs its USB connection checked."),
    PushText("nixos.ups_power_back", "ups", "note", "Power is back", NoActionNeeded(),
        "Mains power is back on the natedev computer. Battery charge: {charge}%.",
        (PushExample("typical", {"charge": "80"}),),
        purpose="Mains power returned to natedev after a battery alert."),
    PushText("nixos.ups_shutdown", "ups", "blocked", "Power outage stopped the computer",
        ActionTemplate("When power returns, start natedev if it stays off for three minutes."),
        "The natedev computer is shutting down because its backup battery is low. "
        + "Work cannot resume until it starts again. It should start automatically when mains power returns.",
        (PushExample("typical", {}),),
        purpose="Power is out, and the battery is low. natedev shuts down."),
    PushText("mac.locked", "{sender}", "blocked", "Mac locked: GPU shots stopped",
        ActionTemplate('Unlock the Mac and keep its display awake, then type "Mac unlocked" in the {sender} tab on natedev'),
        "Every Mac screenshot is black. {waiting}. Code and test work goes on.",
        (PushExample("typical", {"sender": "hana", "waiting": "One unit's phase waits for 2 shots"}),),
        purpose="The Mac is locked and screenshots wait for you to unlock it."),
    PushText("github.keys_cold", "{sender}", "blocked", "GitHub keys locked on {machine}",
        ActionTemplate("Run github-warmup in a terminal on {machine}"), "",
        (PushExample("typical", {"sender": "hana", "machine": "natedev"}),),
        purpose="GitHub keys are cold. Unlock them once for every session."),
    PushText("build_hold.blocking", "{sender}", "blocked", "{machine} build hold is blocking all work",
        ActionTemplate("Say 'release' in the {holder} tab"),
        "{holder} holds {machine} for: {reason}. That work finished at {finished}, but the hold still pauses every other build.",
        (PushExample("typical", {"sender": "hana", "machine": "natedev", "holder": "builds",
                                 "reason": "a compiler check", "finished": "09:10 EST"}),),
        purpose="Finished work left a build hold that you must release."),
    # Agent-written fields: short and to the point; live review selects a fixed text when one fits.
    PushText("alert.blocked", "{sender}", "blocked", "{title}", ActionTemplate("{action}"), "{body}",
        (PushExample("typical", {"sender": "hana", "title": "Hana crashes at start on the Mac",
            "action": "On the Mac, tell the hana tab to roll back or keep the Bevy update",
            "body": "Hana stops at start with a shader error since the Bevy update at 09:10. Mac screenshots wait until it starts again."}),),
        purpose="Work stopped. Write title, action and body short and to the point.", agent_written=True),
    PushText("alert.decision", "{sender}", "decision", "{title}", ActionTemplate("{action}"), "{body}",
        (PushExample("typical", {"sender": "builds", "title": "Keep the old build cache?",
            "action": "Say keep or delete in the builds tab",
            "body": "The old build cache uses 120 GiB. Deleting it frees the space; the next build of each repo takes about 10 minutes longer."}),),
        purpose="A choice for you. Write title, action and body short and to the point.", agent_written=True),
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
    # Auto-mode refusal (texts from 2026-11-06, on the user's question via natedev): a session sends it with send.py --text-id auto_mode.refused.
    # send.py fills sender from the session's own name, action from the commands' first words (rca.action_of),
    # refused from the command values (one per line), waits_line from waits when the 30 s push review passes it
    # (else ""), noun and this from the number of commands, and rule_line from rule.
    PushText("auto_mode.refused", "{sender}", "decision", "{sender}: {session} {noun} refused",
        ActionTemplate("On {machine}, in the {session} pane, type: Go: run the refused {noun}"),
        "Auto mode refused {action} in the {session} session.{waits_line}\n{refused}\n\n"
        + "Go: the session runs {this} {noun} now, and auto mode can refuse {this} {noun} again.\n\n"
        + "Allow rule: auto mode lets {this} {noun} run from then on.{rule_line}",
        (PushExample("typical", {"sender": "cargo-liner-style", "session": "modules", "machine": "natedev",
            "action": "2 shell commands", "refused": "implement.sh --to modules-impl\nreview.sh --to modules-test",
            "noun": "commands", "this": "these", "waits_line": " The modules build waits for them.", "rule_line": ""}),
         PushExample("one_with_rule", {"sender": "build-followups", "session": "messaging", "machine": "natedev",
            "action": "a shell command", "refused": "git push origin build-followups-messaging", "noun": "command",
            "this": "this", "waits_line": " The merge of the messaging work waits for it.", "rule_line": " The rule: Bash(git push:*)"}),
         PushExample("file_change_no_waits", {"sender": "hana", "session": "demo", "machine": "natedev",
            "action": "a file change", "refused": "write phases/34h/delegated_phase_reservation_state.json",
            "noun": "command", "this": "this", "waits_line": "", "rule_line": ""})),
        purpose="Auto mode refused a session's commands. The commands wait for you.",
        session=SessionTemplate("{session}")),
    # Permission root cause (user ask, 2026-10-23): rca.py sends these when the owner of a case starts it
    # (`rca.py start`) and when its fix merges (`rca.py fixed`), with --key rca-<case id>. sender is the
    # session's production, or messaging when it has none. rca.py fills time (silence.shown) and stop from the case.
    # It removes a final period from cause and fix. Only the started push has the alert's waits_line.
    # No owner, file, command or commit (user, 2026-10-23 19:20). Examples: the demo and handler cases of 2026-10-23.
    PushText("permission.rca_started", "{sender}", "note", "{sender}: finding why {session} hit a check",
        NoActionNeeded(),
        "At {time} in the {session} session, {stop}.{waits_line} "
        + "A session now finds the cause and fixes it, so this does not happen again. "
        + "A second push comes when the fix is in.",
        (PushExample("typical", {"sender": "hana", "session": "demo", "time": "14:23 AST",
            "stop": "auto mode refused a file change", "waits_line": " The demo build waits for it."}),
         PushExample("prompt", {"sender": "cargo-liner", "session": "handler", "time": "09:01 AST",
            "stop": "the session asked you to allow a shell command", "waits_line": ""})),
        purpose="Someone starts to find the cause of a session's permission check."),
    PushText("permission.rca_fixed", "{sender}", "note", "{sender}: fixed why {session} hit a check",
        NoActionNeeded(),
        "At {time} in the {session} session, {stop}.\nWhy: {cause}.\nNow: {fix}.",
        (PushExample("typical", {"sender": "hana", "session": "demo", "time": "14:23 AST",
            "stop": "auto mode refused a file change",
            "cause": "the steps told the session to write a progress file by hand, which auto mode does not allow",
            "fix": "a script writes that file, so the session never has to"}),
         PushExample("prompt", {"sender": "cargo-liner", "session": "handler", "time": "09:01 AST",
            "stop": "the session asked you to allow a shell command",
            "cause": "a helper ran a build command that needs your permission",
            "fix": "helpers use the build command that is always allowed"})),
        purpose="The fix merged: that check no longer stops or refuses the session."),
    # rca.py fixed sends this when the push review refused the fixed text twice: the reviewer's own rewrite.
    PushText("permission.rca_fixed_rewritten", "{sender}", "note", "{title}", NoActionNeeded(), "{body}",
        (PushExample("typical", {"sender": "hana", "title": "hana: fixed why demo hit a check",
            "body": "At 14:23 AST in the demo session, auto mode refused a file change.\n"
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

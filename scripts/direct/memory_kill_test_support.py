"""Incident fixtures shared by memory-kill classifier and shell retry tests."""

import json
from pathlib import Path


KILL_AT = 1793459938.617134
# Exact command/message from the 2026-10-31 11:18:58 hana incident.
RUSTC_COMMAND = "/home/natepiano/.rustup/toolchains/1.99.0-x86_64-unknown-linux-gnu/bin/rustc --crate-name hana --edition=2024 crates/hana/src/main.rs --error-format=json --json=diagnostic-rendered-ansi,artifacts,future-incompat --emit=dep-info,link -C embed-bitcode=no -"
EARLYOOM_MESSAGE = ('sending SIGTERM to process 1202214 uid 1000 "rustc": oom_score 1064, '
                    + 'oom_score_adj 500, VmRSS 9855 MiB, cmdline "' + RUSTC_COMMAND + '"')


def failed_compile_log(signal_text: str = "") -> str:
    return ('error: could not compile `hana` (bin "hana" test)\n\nCaused by:\n'
            + "  process didn't exit successfully: `sccache " + RUSTC_COMMAND + "` (exit status: 2)\n"
            + (signal_text + "\n" if signal_text else ""))


def journal_event(message: str = EARLYOOM_MESSAGE, at: float = KILL_AT) -> str:
    return json.dumps({"__REALTIME_TIMESTAMP": str(round(at * 1_000_000)), "MESSAGE": message}) + "\n"


def install_journal(root: Path, journal: str) -> None:
    data = root / "journal.jsonl"
    _ = data.write_text(journal)
    stub = root / "journalctl"
    _ = stub.write_text('#!/bin/sh\ncat "' + str(data) + '"\n')
    stub.chmod(0o755)

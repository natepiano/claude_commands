"""Release checks with all external effects replaced by local fixtures."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import tempfile
import time
import unittest
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import final, override
from unittest import mock

import rust_release
from test_index import point_root_at


def channel(version: str = "1.100.0", day: str = "2026-11-12") -> bytes:
    return f'date = "{day}"\n[pkg.rust]\nversion = "{version} (abcdef 2026-11-12)"\n'.encode()


def compiler_message(package: str, level: str, code: str, line: int) -> str:
    return json.dumps({
        "reason": "compiler-message",
        "package_id": package,
        "message": {
            "level": level,
            "code": {"code": code},
            "spans": [{"file_name": "src/lib.rs", "line_start": line, "column_start": 1, "is_primary": True}],
        },
    })


@final
class FakeRunner:
    def __init__(self, clone: Path, *, fail: str = "", timeout: str = "", diagnostics: str = "", clippy_exit: int = 0, killed: str = "", kill_status: int = 137, kill_output: str = "") -> None:
        self.clone = clone
        self.fail = fail
        self.timeout = timeout
        self.diagnostics = diagnostics
        self.clippy_exit = clippy_exit
        self.killed = killed
        self.kill_status = kill_status
        self.kill_output = kill_output
        self.calls: list[tuple[list[str], Path | None, dict[str, str], int]] = []

    def __call__(self, args: list[str], cwd: Path | None, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, cwd, env, timeout))
        step = "toolchain" if args[0] == "rustup" else "clone" if args[:2] == ["git", "clone"] else "revision" if "rev-parse" in args else "checkout" if "checkout" in args else "clippy" if args[0] == "nix" else "cargo-mend"
        if step == "clone":
            self.clone.mkdir()
        if step == "clippy":
            target = self.clone / "target"
            target.mkdir()
            _ = (target / "output").write_bytes(b"clippy")
        if step == "cargo-mend":
            target = self.clone / "mend-target"
            target.mkdir()
            _ = (target / "output").write_bytes(b"mend")
        if step == self.timeout:
            raise subprocess.TimeoutExpired(args, timeout)
        if step == self.fail:
            return subprocess.CompletedProcess(args, 1, "", f"error: {step} broke\n")
        if step == self.killed:
            return subprocess.CompletedProcess(args, self.kill_status, "", self.kill_output)
        output = "abc123\n" if step == "revision" else self.diagnostics if step == "clippy" else ""
        return subprocess.CompletedProcess(args, self.clippy_exit if step == "clippy" else 0, output, "")


class RustReleaseTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    scratch: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    config: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary) / "buildlog"
        self.scratch = Path(temporary) / "scratch"
        self.scratch.mkdir()
        self.config = Path(temporary) / "lint.conf"
        _ = self.config.write_text("sweep_free_floor_gib = 500\n")
        _ = self.enterContext(mock.patch.object(rust_release, "LINT_CONFIG", self.config))
        _ = self.enterContext(mock.patch.object(tempfile, "gettempdir", return_value=str(self.scratch)))
        point_root_at(self, self.root)

    def invoke(
        self,
        *,
        pin: str | None = "1.99.0",
        at: datetime | None = None,
        version: str = "1.100.0",
        free: float = 600,
        held: bool = False,
        building: bool = False,
        runner: rust_release.RunCommand | None = None,
        held_check: Callable[[], bool] | None = None,
        texts: list[tuple[str, str]] | None = None,
        fetches: list[str] | None = None,
        fetch_channel: Callable[[], bytes] | None = None,
    ) -> int:
        def fetch() -> bytes:
            if fetches is not None:
                fetches.append(version)
            return channel(version)

        def send(title: str, message: str) -> bool:
            if texts is not None:
                texts.append((title, message))
            return True

        def no_run(args: list[str], cwd: Path | None, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
            _ = (cwd, env, timeout)
            self.fail(f"unexpected subprocess: {args}")

        return rust_release.check_release(
            fetch_channel=fetch_channel or fetch,
            read_pin=lambda: pin,
            run_command=runner or no_run,
            now=lambda: at or datetime(2026, 11, 12, 3),
            free_gib=lambda: free,
            held=held_check or (lambda: held),
            building=lambda: building,
            send_text=send,
        )

    def test_no_pin_is_silent_without_fetch_or_text(self) -> None:
        fetches: list[str] = []
        texts: list[tuple[str, str]] = []
        self.assertEqual(0, self.invoke(pin=None, fetches=fetches, texts=texts))
        self.assertEqual([], fetches)
        self.assertEqual([], texts)
        self.assertFalse(rust_release.state_path().exists())

    def test_same_version_is_silent(self) -> None:
        texts: list[tuple[str, str]] = []
        self.assertEqual(0, self.invoke(pin="1.100.0", texts=texts))
        self.assertEqual([], texts)
        self.assertEqual([], rust_release.report_line())

    def test_newer_stable_is_fetched_once_per_local_day(self) -> None:
        fetches: list[str] = []
        self.assertEqual(0, self.invoke(at=datetime(2026, 11, 12, 12), fetches=fetches))
        self.assertEqual(0, self.invoke(at=datetime(2026, 11, 12, 13), fetches=fetches))
        self.assertEqual(["1.100.0"], fetches)
        state = rust_release.read_state()
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual("2026-11-12", state["check_day"])
        self.assertEqual("1.100.0", state["stable_version"])
        self.assertEqual("2026-11-12", state["release_date"])
        self.assertEqual(0, self.invoke(at=datetime(2026, 11, 13, 12), fetches=fetches))
        self.assertEqual(["1.100.0", "1.100.0"], fetches)

    def test_failed_channel_fetch_records_error_and_retries_next_hour(self) -> None:
        attempts = 0

        def fetch() -> bytes:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("channel unavailable")
            return channel()

        def no_run(args: list[str], cwd: Path | None, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
            _ = (cwd, env, timeout)
            self.fail(f"unexpected subprocess: {args}")

        for hour, expected in ((12, 1), (13, 0)):
            self.assertEqual(expected, rust_release.check_release(
                fetch_channel=fetch, read_pin=lambda: "1.99.0", run_command=no_run,
                now=lambda hour=hour: datetime(2026, 11, 12, hour), free_gib=lambda: 600,
                held=lambda: False, building=lambda: False, send_text=lambda title, message: True,
            ))
        self.assertEqual(2, attempts)
        self.assertIn("channel unavailable", (self.root / "errors.log").read_text())

    def test_pin_disappearing_after_pending_release_hides_report_line(self) -> None:
        self.assertEqual(0, self.invoke(at=datetime(2026, 11, 12, 12)))
        self.assertEqual(1, len(rust_release.report_line()))
        self.assertEqual(0, self.invoke(pin=None, at=datetime(2026, 11, 12, 13)))
        self.assertEqual([], rust_release.report_line())

    def test_hold_waits_with_reason_and_sends_one_text(self) -> None:
        texts: list[tuple[str, str]] = []
        self.assertEqual(0, self.invoke(held=True, texts=texts))
        self.assertEqual(0, self.invoke(held=True, texts=texts))
        self.assertEqual(1, len(texts))
        self.assertEqual("Rust 1.100.0 out", texts[0][0])
        self.assertIn("trial waiting: build hold active", texts[0][1])
        state = rust_release.read_state()
        assert state is not None
        self.assertEqual("waiting", state["trial"]["status"])

    def test_existing_cargo_build_waits_with_reason(self) -> None:
        self.assertEqual(0, self.invoke(building=True))
        state = rust_release.read_state()
        assert state is not None
        assert state["trial"]["status"] == "waiting"
        self.assertEqual("cargo build active", state["trial"]["reason"])

    def test_low_disk_waits_with_floor_plus_headroom(self) -> None:
        self.assertEqual(0, self.invoke(free=513))
        state = rust_release.read_state()
        assert state is not None
        assert state["trial"]["status"] == "waiting"
        self.assertEqual("513 GiB free, needs 550", state["trial"]["reason"])

    def test_before_quiet_hours_waits_and_sends_no_text(self) -> None:
        texts: list[tuple[str, str]] = []
        self.assertEqual(0, self.invoke(at=datetime(2026, 11, 12, 1), texts=texts))
        self.assertEqual([], texts)
        state = rust_release.read_state()
        assert state is not None
        assert state["trial"]["status"] == "waiting"
        self.assertEqual("awaiting quiet hours", state["trial"]["reason"])

    def test_diagnostic_counts_deduplicate_targets_and_count_crates(self) -> None:
        lines = [
            compiler_message("alpha", "warning", "unused", 1),
            compiler_message("alpha", "warning", "unused", 1),
            compiler_message("alpha", "warning", "dead_code", 4),
            compiler_message("beta", "warning", "unused", 1),
            compiler_message("beta", "error", "E001", 8),
            '{"reason":"build-script-executed"}',
            "not json",
        ]
        self.assertEqual((3, 2, 1, 1), rust_release.diagnostic_counts("\n".join(lines)))

    def test_compiler_message_json_error_uses_first_rendered_line(self) -> None:
        diagnostic = json.dumps({
            "reason": "compiler-message",
            "message": {
                "level": "error",
                "message": "fallback message",
                "rendered": "error[E0554]: feature may not be used\n  --> src/lib.rs:1:1\n",
            },
        })
        result = subprocess.CompletedProcess(["nix", "develop"], 1, diagnostic + "\n", "")
        self.assertEqual("error[E0554]: feature may not be used", rust_release.first_error(result))

    def test_finished_trial_records_counts_sizes_and_one_result_text(self) -> None:
        diagnostics = "\n".join([
            compiler_message("alpha", "warning", "unused", 1),
            compiler_message("alpha", "warning", "unused", 1),
            compiler_message("beta", "warning", "dead_code", 2),
            compiler_message("beta", "error", "E001", 3),
        ])
        clone = self.scratch / "rust-release-trial-1.100.0"
        runner = FakeRunner(clone, diagnostics=diagnostics)
        texts: list[tuple[str, str]] = []
        self.assertEqual(0, self.invoke(runner=runner, texts=texts))
        state = rust_release.read_state()
        assert state is not None
        trial = state["trial"]
        assert trial["status"] == "finished"
        self.assertEqual((2, 2, 1, 1), (trial["warnings"], trial["warning_crates"], trial["errors"], trial["error_crates"]))
        self.assertTrue(trial["mend_builds"])
        self.assertGreater(trial["target_gib"], 0)
        self.assertGreater(trial["mend_target_gib"], 0)
        self.assertFalse(clone.exists())
        self.assertEqual(["rustup", "git", "git", "git", "nix", "cargo"], [args[0] for args, _, _, _ in runner.calls])
        clippy = next(call for call in runner.calls if call[0][0] == "nix")
        self.assertEqual(clone, clippy[1])
        self.assertEqual(7200, clippy[3])
        self.assertEqual("0", clippy[2]["CARGO_INCREMENTAL"])
        self.assertEqual("-C link-arg=-fuse-ld=mold", clippy[2]["CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS"])
        mend = runner.calls[-1]
        self.assertEqual("1", mend[2]["RUSTC_BOOTSTRAP"])
        self.assertEqual(str(clone / "mend-target"), mend[2]["CARGO_TARGET_DIR"])
        self.assertEqual(1, len(texts))
        self.assertIn("2 new warnings in 2 crates", texts[0][1])
        self.assertIn("1 error in 1 crate", texts[0][1])
        self.assertIn("cargo-mend builds", texts[0][1])

    def test_stale_trial_clone_is_removed_before_commands_and_after_success(self) -> None:
        clone = self.scratch / "rust-release-trial-1.100.0"
        clone.mkdir()
        _ = (clone / "stale-output").write_text("old build")
        fake = FakeRunner(clone)

        def run(args: list[str], cwd: Path | None, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
            if args[0] == "rustup":
                self.assertFalse(clone.exists())
            return fake(args, cwd, env, timeout)

        self.assertEqual(0, self.invoke(runner=run))
        self.assertFalse(clone.exists())
        state = rust_release.read_state()
        assert state is not None
        self.assertEqual("finished", state["trial"]["status"])

    def test_mend_nonzero_finishes_and_text_says_does_not_build(self) -> None:
        clone = self.scratch / "rust-release-trial-1.100.0"
        runner = FakeRunner(clone, fail="cargo-mend")
        texts: list[tuple[str, str]] = []
        self.assertEqual(0, self.invoke(runner=runner, texts=texts))
        state = rust_release.read_state()
        assert state is not None
        trial = state["trial"]
        assert trial["status"] == "finished"
        self.assertFalse(trial["mend_builds"])
        self.assertFalse(clone.exists())
        self.assertIn("cargo-mend does not build", texts[0][1])

    def test_failed_clippy_step_records_first_error_and_is_not_retried(self) -> None:
        clone = self.scratch / "rust-release-trial-1.100.0"
        runner = FakeRunner(clone, fail="clippy")
        self.assertEqual(0, self.invoke(runner=runner))
        state = rust_release.read_state()
        assert state is not None
        trial = state["trial"]
        assert trial["status"] == "failed"
        self.assertEqual(("clippy", "error: clippy broke"), (trial["step"], trial["reason"]))
        self.assertFalse(clone.exists())
        calls = len(runner.calls)
        self.assertEqual(0, self.invoke(runner=runner))
        self.assertEqual(calls, len(runner.calls))

    def test_killed_trial_waits_and_retries_next_night(self) -> None:
        clone = self.scratch / "rust-release-trial-1.100.0"
        for status, output in ((137, ""), (1, "process failed: signal: 9\n"), (1, "process failed: signal 15\n"), (143, "")):
            with self.subTest(status=status, output=output):
                rust_release.state_path().unlink(missing_ok=True)
                killed = FakeRunner(clone, killed="clippy", kill_status=status, kill_output=output)
                self.assertEqual(0, self.invoke(runner=killed))
                state = rust_release.read_state()
                assert state is not None
                self.assertEqual("waiting", state["trial"]["status"])
                self.assertFalse(clone.exists())
                retry = FakeRunner(clone)
                self.assertEqual(0, self.invoke(at=datetime(2026, 11, 13, 3), runner=retry))
                state = rust_release.read_state()
                assert state is not None
                self.assertEqual("finished", state["trial"]["status"])
                self.assertEqual("cargo", retry.calls[-1][0][0])

    def test_hold_arriving_mid_trial_deletes_clone_and_leaves_pending(self) -> None:
        clone = self.scratch / "rust-release-trial-1.100.0"
        runner = FakeRunner(clone)
        checks = 0

        def held() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 5

        self.assertEqual(0, self.invoke(runner=runner, held_check=held))
        state = rust_release.read_state()
        assert state is not None
        trial = state["trial"]
        assert trial["status"] == "waiting"
        self.assertEqual("build hold active", trial["reason"])
        self.assertFalse(clone.exists())
        self.assertEqual(["rustup", "git"], [args[0] for args, _, _, _ in runner.calls])

    def test_timeout_deletes_clone_and_records_failed_step(self) -> None:
        clone = self.scratch / "rust-release-trial-1.100.0"
        runner = FakeRunner(clone, timeout="clippy")
        self.assertEqual(0, self.invoke(runner=runner))
        state = rust_release.read_state()
        assert state is not None
        trial = state["trial"]
        assert trial["status"] == "failed"
        self.assertEqual("clippy", trial["step"])
        self.assertFalse(clone.exists())

    def test_waiting_text_is_not_repeated_after_later_trial_or_new_day(self) -> None:
        texts: list[tuple[str, str]] = []
        self.assertEqual(0, self.invoke(held=True, texts=texts))
        runner = FakeRunner(self.scratch / "rust-release-trial-1.100.0")
        self.assertEqual(0, self.invoke(runner=runner, texts=texts))
        self.assertEqual(0, self.invoke(at=datetime(2026, 11, 13, 3), runner=runner, texts=texts))
        self.assertEqual(1, len(texts))
        state = rust_release.read_state()
        assert state is not None
        self.assertEqual("finished", state["trial"]["status"])

    def test_new_version_gets_its_own_text(self) -> None:
        texts: list[tuple[str, str]] = []
        self.assertEqual(0, self.invoke(held=True, texts=texts))
        self.assertEqual(0, self.invoke(version="1.101.0", at=datetime(2026, 11, 13, 3), held=True, texts=texts))
        self.assertEqual(["Rust 1.100.0 out", "Rust 1.101.0 out"], [title for title, _ in texts])

    def test_clippy_diagnostics_finish_trial_but_non_diagnostic_exit_fails(self) -> None:
        clone = self.scratch / "rust-release-trial-1.100.0"
        diagnostic = compiler_message("alpha", "error", "E001", 3)
        runner = FakeRunner(clone, diagnostics=diagnostic, clippy_exit=101)
        texts: list[tuple[str, str]] = []
        self.assertEqual(0, self.invoke(runner=runner, texts=texts))
        state = rust_release.read_state()
        assert state is not None
        trial = state["trial"]
        assert trial["status"] == "finished"
        self.assertEqual((1, 1), (trial["errors"], trial["error_crates"]))
        self.assertEqual("cargo", runner.calls[-1][0][0])
        self.assertIn("1 error in 1 crate", texts[0][1])
        self.assertIn("cargo-mend builds", texts[0][1])
        self.assertIn("1 error in 1 crate, cargo-mend builds", rust_release.report_line()[0])

        rust_release.state_path().unlink()
        failed_runner = FakeRunner(clone, fail="clippy")
        self.assertEqual(0, self.invoke(runner=failed_runner))
        state = rust_release.read_state()
        assert state is not None
        trial = state["trial"]
        assert trial["status"] == "failed"
        self.assertEqual(("clippy", "error: clippy broke"), (trial["step"], trial["reason"]))
        self.assertEqual("nix", failed_runner.calls[-1][0][0])

    def test_failed_daily_fetch_refreshes_pin_and_continues_saved_trial(self) -> None:
        def unavailable() -> bytes:
            raise OSError("channel unavailable")

        self.assertEqual(0, self.invoke(at=datetime(2026, 11, 12, 12)))
        self.assertEqual(1, self.invoke(pin="1.100.0", at=datetime(2026, 11, 13, 12), fetch_channel=unavailable))
        state = rust_release.read_state()
        assert state is not None
        self.assertEqual("2026-11-12", state["check_day"])
        self.assertEqual("1.100.0", state["pin"])
        self.assertEqual([], rust_release.report_line())

        self.assertEqual(0, self.invoke(pin="1.99.0", at=datetime(2026, 11, 12, 12)))
        runner = FakeRunner(self.scratch / "rust-release-trial-1.100.0")
        texts: list[tuple[str, str]] = []
        self.assertEqual(1, self.invoke(at=datetime(2026, 11, 13, 3), runner=runner, texts=texts, fetch_channel=unavailable))
        state = rust_release.read_state()
        assert state is not None
        self.assertEqual("2026-11-12", state["check_day"])
        self.assertEqual("finished", state["trial"]["status"])
        self.assertTrue(state["text_sent"])
        self.assertEqual(1, len(texts))
        self.assertEqual("cargo", runner.calls[-1][0][0])

    def test_hold_after_nonzero_clippy_keeps_trial_waiting_and_deletes_clone(self) -> None:
        clone = self.scratch / "rust-release-trial-1.100.0"
        runner = FakeRunner(clone, fail="clippy")

        def held() -> bool:
            return any(args[0] == "nix" for args, _, _, _ in runner.calls)

        self.assertEqual(0, self.invoke(runner=runner, held_check=held))
        state = rust_release.read_state()
        assert state is not None
        self.assertEqual({"status": "waiting", "version": "1.100.0", "reason": "build hold active"}, state["trial"])
        self.assertFalse(clone.exists())
        self.assertEqual("nix", runner.calls[-1][0][0])

    def test_missing_pin_keeps_finished_trial_and_sent_text_for_return(self) -> None:
        clone = self.scratch / "rust-release-trial-1.100.0"
        runner = FakeRunner(clone)
        texts: list[tuple[str, str]] = []
        self.assertEqual(0, self.invoke(runner=runner, texts=texts))
        original = rust_release.read_state()
        assert original is not None
        self.assertEqual("finished", original["trial"]["status"])
        fetches: list[str] = []
        self.assertEqual(0, self.invoke(pin=None, fetches=fetches, texts=texts))
        absent = rust_release.read_state()
        assert absent is not None
        self.assertIsNone(absent["pin"])
        self.assertEqual(original["trial"], absent["trial"])
        self.assertTrue(absent["text_sent"])
        self.assertEqual([], fetches)
        self.assertEqual([], rust_release.report_line())
        calls = len(runner.calls)
        self.assertEqual(0, self.invoke(runner=runner, texts=texts))
        self.assertEqual(calls, len(runner.calls))
        self.assertEqual(1, len(texts))
        returned = rust_release.read_state()
        assert returned is not None
        self.assertEqual("1.99.0", returned["pin"])

    def test_build_hold_is_active_while_any_holder_file_remains(self) -> None:
        folder = self.scratch / "build-hold"
        folder.mkdir()
        self.assertFalse(rust_release.build_hold_active(folder))
        (folder / "directory").mkdir()
        self.assertFalse(rust_release.build_hold_active(folder))
        first = folder / "holder-one"
        second = folder / "holder-two"
        _ = first.write_text("holder one, 2026-11-12T03:00:00, test one\n")
        _ = second.write_text("holder two, 2026-11-12T03:00:00, test two\n")
        self.assertTrue(rust_release.build_hold_active(folder))
        first.unlink()
        self.assertTrue(rust_release.build_hold_active(folder))
        second.unlink()
        self.assertFalse(rust_release.build_hold_active(folder))

    def test_json_holder_file_is_still_an_active_build_hold(self) -> None:
        folder = self.scratch / "build-hold"
        folder.mkdir()
        holder = folder / "slot-one"
        _ = holder.write_text(json.dumps({
            "holder": "slot one",
            "since": "2026-11-12T03:00:00-05:00",
            "for": "the focused test",
            "release_eta": "unknown",
        }) + "\n")
        self.assertTrue(rust_release.build_hold_active(folder))
        holder.unlink()
        self.assertFalse(rust_release.build_hold_active(folder))

    def test_two_part_pin_covers_patch_releases_and_channel_names_are_absent(self) -> None:
        texts: list[tuple[str, str]] = []
        self.assertEqual(0, self.invoke(pin="1.99", version="1.99.1", texts=texts))
        self.assertEqual([], rust_release.report_line())
        self.assertEqual([], texts)
        self.assertEqual(0, self.invoke(pin="1.99", version="1.100.0", at=datetime(2026, 11, 13, 12)))
        self.assertIn("Rust 1.100.0 out", rust_release.report_line()[0])
        self.assertEqual(0, self.invoke(pin="stable", fetches=[]))
        state = rust_release.read_state()
        assert state is not None
        self.assertIsNone(state["pin"])
        self.assertEqual([], rust_release.report_line())
        result = subprocess.CompletedProcess(["git"], 0, '[toolchain]\nchannel = "nightly-2026-10-01"\n', "")
        with mock.patch("rust_release.subprocess.run", return_value=result):
            self.assertIsNone(rust_release.pinned_version())

    def test_timed_out_process_kills_background_child(self) -> None:
        pid_file = self.scratch / "sleep.pid"
        args = ["sh", "-c", f"sleep 60 & echo $! > {shlex.quote(str(pid_file))}; wait"]
        with self.assertRaises(subprocess.TimeoutExpired):
            _ = rust_release.run_process(args, self.scratch, dict(os.environ), 1)
        pid = int(pid_file.read_text().strip())
        try:
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                self.fail(f"background sleep {pid} still exists after timeout")
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

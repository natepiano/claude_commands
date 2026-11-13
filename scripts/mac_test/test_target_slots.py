"""Check repository identity, slot ownership, and worktree affinity."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from types import FrameType
from typing import cast, override

from scripts.build_hold.owning_session import NoOwningSession
from scripts.lib import process_start as process_identity
from scripts.lib.git_test_support import commit_repository
from scripts.lib.process_start_test_support import process_reader
from scripts.copies.copies_test_support import copies_environment

import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]

from scripts.mac_test import claim_state, mac_test, offload
from scripts.build_hold import build_hold
from scripts.build_targets import targets
from scripts.lint.memory_test_support import config_environment, write_config_fixture


class RepositoryKeyTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "project"
        _ = subprocess.run(["git", "init", "--quiet", str(self.root)], check=True, capture_output=True)

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *arguments], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_real_origin_forms_keep_the_normalized_repository_key(self) -> None:
        expected = offload.KnownRepository("project", "https://github.com/nate/project")
        for remote in ("git@GitHub.com:nate/project.git", "https://github.com/nate/project.git"):
            with self.subTest(remote=remote):
                _ = self.git("config", "remote.origin.url", remote)
                self.assertEqual(offload.repository_identity(self.root), expected)

    def test_real_repository_without_origin_keeps_the_root_commit_key(self) -> None:
        root_commit = commit_repository(self.root, "Fixture", allow_empty=True)
        self.assertEqual(offload.repository_identity(self.root),
                         offload.KnownRepository("project", "git-root:" + root_commit))

    def test_real_repository_without_commits_keeps_the_repository_name(self) -> None:
        self.assertEqual(offload.repository_identity(self.root), offload.KnownRepository("project"))

    def test_attempt_without_commits_or_origin_records_unknown_identity(self) -> None:
        log_root = self.root.parent / "buildlog"
        request = offload.OffloadRequest(self.root, "project", "empty-repository", offload.NO_RESULT_FILE, (), owner="verify",
                                        target="linux", holder_session="fixture-session")
        with mock.patch.dict(os.environ, {
            "BUILDLOG_DIR": str(log_root), "BUILDLOG_MEM_WAIT_SINCE": "", "BUILDLOG_MEM_WAIT_FIRST_FILE": "",
        }):
            offload.persist_attempt(request, offload.DeclinedMacRun("off", 0),
                                    offload.AttemptEvidence(), time.monotonic())
        rows = [cast(dict[str, object], json.loads(line))
                for path in log_root.rglob("*.jsonl") for line in path.read_text().splitlines()]
        self.assertEqual([row["repo_identity"] for row in rows if row["kind"] == "ssh_attempt"], ["unknown"])

    def test_repository_key_lookup_runs_no_memory_admit_code(self) -> None:
        files: set[str] = set()

        def collect_file(frame: FrameType, event: str, argument: object) -> None:
            if event == "call" and argument is None:
                files.add(frame.f_code.co_filename)

        sys.setprofile(collect_file)
        try:
            _ = offload.repository_identity(self.root)
        finally:
            sys.setprofile(None)
        self.assertNotIn("memory_admit.py", {Path(file).name for file in files})


class RepositorySlotTests(unittest.TestCase):
    root: Path = Path()
    origins: dict[str, str] = {}
    common: dict[str, Path] = {}
    dead: set[int] = set()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.origins = {}
        self.common = {}
        self.dead = set()
        settings = (
            "offload=on\nci_repo=\nblock_hours=4\nblock_max_hours=48\n"
            "block_warn_minutes=15\ngh_timeout_s=1\nclaim_idle_minutes=15\n"
            "claim_poll_seconds=60\nconnect_timeout_s=1\nprobe_timeout_s=1\n"
            "unreachable_backoff_s=30\nslots=2\nslots.hana=4\n"
            "build_budget_gib=32\ntarget_budget_gib=24\n"
        )
        for name in ("mac", "linux"):
            _ = write_config_fixture(self.root / f"{name}.conf", f"host=fake-{name}\n" + settings,
                                     template="mac_test")
        _ = write_config_fixture(self.root / "mac_test.conf", "ci_repo=", template="mac_test")
        registry = self.root / "build_targets.conf"
        _ = registry.write_text(
            "targets=mac,linux,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac.conf\n"
            + "kind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n"
        )
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **config_environment(self.root),
            **copies_environment(self.root),
            "HOME": str(self.root), "BUILD_TARGETS_CONFIG": str(registry),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILD_HOLD_DIR": str(self.root / "holders"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
        })))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = self.enterContext(mock.patch.object(process_identity, "process_start", side_effect=process_reader(self.process_start)))
        _ = self.enterContext(mock.patch.object(mac_test, "utc_now", return_value=datetime(2026, 11, 1, tzinfo=timezone.utc)))
        _ = self.enterContext(mock.patch.object(time, "monotonic", return_value=100.0))
        _ = self.enterContext(mock.patch.object(build_hold, "owning_session", return_value=NoOwningSession()))
        _ = self.enterContext(mock.patch.object(mac_test, "start_claim_watcher"))
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=self.git_reply))
        _ = self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected child process")))

    def process_start(self, pid: int) -> str | None:
        return None if pid in self.dead else f"start-{pid}"

    def repository(self, name: str, origin: str, source: Path | None = None) -> Path:
        root = self.root / name
        root.mkdir(parents=True)
        if source is None:
            common = root / ".git"
            common.mkdir()
            _ = (common / "config").write_text(f'[remote "origin"]\n\turl = {origin}\n')
        else:
            common = self.common[str(source)]
            private = common / "worktrees" / root.name
            private.mkdir(parents=True)
            _ = (private / "commondir").write_text("../..\n")
            _ = (root / ".git").write_text(f"gitdir: {private}\n")
        self.origins[str(root)] = origin
        self.common[str(root)] = common
        return root

    def git_reply(self, argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        self.assertEqual(argv[0], "git", f"Unexpected external command: {argv}")
        directory = argv[argv.index("-C") + 1] if "-C" in argv else str(_options.get("cwd", ""))
        origin = self.origins.get(directory)
        if origin is None:
            return subprocess.CompletedProcess(argv, 128, "", "not a repository")
        if "remote" in argv or "remote.origin.url" in argv:
            answer = origin
        elif "--git-common-dir" in argv:
            answers = [str(self.common[directory])]
            if "--show-toplevel" in argv:
                answers.append(directory)
            if "--abbrev-ref" in argv:
                answers.append("main")
            answer = "\n".join(answers)
        elif "--show-toplevel" in argv:
            answer = directory
        elif "--git-dir" in argv:
            answer = str(self.common[directory])
        elif "rev-list" in argv or "HEAD" in argv:
            answer = "a" * 40
        else:
            self.fail(f"Unexpected git command: {argv}")
        return subprocess.CompletedProcess(argv, 0, answer + "\n", "")

    def arguments(self, target: str, worktree: Path, pid: int, remote: bool = False) -> mac_test.CliArguments:
        args = mac_test.CliArguments()
        args.target = target
        args.worktree = str(worktree)
        args.pid = pid
        args.what = f"check {worktree.name}"
        if remote:
            args.remote = f"fake-{target}"
        return args

    def claim(self, target: str, worktree: Path, pid: int, remote: bool = False,
              *, identity: bool = False,
              request_identity: claim_state.ClaimIdentity | claim_state.NoClaimIdentity = claim_state.NO_CLAIM_IDENTITY,
              ) -> tuple[int, str]:
        output = io.StringIO()
        args = self.arguments(target, worktree, pid, remote)
        args.identity = identity
        if isinstance(request_identity, claim_state.ClaimIdentity):
            args.claim_id, args.episode, args.attempt = request_identity.claim_id, request_identity.episode, request_identity.attempt
        with redirect_stdout(output):
            status = mac_test.command_claim(args, mac_test.state_paths(target))
        return status, output.getvalue()

    def release(self, target: str, pid: int) -> None:
        args = mac_test.CliArguments()
        args.target, args.pid = target, pid
        claim = self.claims(target)[pid]
        args.claim_id = cast(str, claim["claim_id"])
        args.episode = cast(str, claim["episode"])
        args.attempt = cast(str, claim["attempt"])
        self.assertEqual(mac_test.command_release(args, mac_test.state_paths(target)), 0)

    def claims(self, target: str) -> dict[int, dict[str, object]]:
        return {claim.record["pid"]: cast(dict[str, object], cast(object, claim.record))
                for claim in mac_test.read_claims(mac_test.state_paths(target))}

    def test_origin_forms_and_clone_names_share_repository_capacity(self) -> None:
        main = self.repository("hana", "git@GitHub.com:nate/hana.git")
        clone = self.repository("tmp/phase-copy", "https://github.com/nate/hana/")
        linked = self.repository("worktrees/feature", "ssh://git@github.com:22/nate/hana.git", main)

        identities = [offload.repository_identity(path) for path in (main, clone, linked)]

        known = [identity for identity in identities if isinstance(identity, offload.KnownRepository)]
        self.assertEqual(len(known), 3)
        self.assertEqual(len({identity.key for identity in known}), 1)
        self.assertEqual([identity.name for identity in known], ["hana", "phase-copy", "hana"])
        self.assertIsInstance(identities[0], offload.KnownRepository)
        self.assertEqual(cast(offload.KnownRepository, identities[0]).name, "hana")
        self.assertEqual(self.claim("mac", main, 101)[0], 0)
        self.assertEqual(self.claim("mac", clone, 102)[0], 0)

    def test_linux_clones_with_different_names_share_origin_capacity(self) -> None:
        main = self.repository("project", "git@github.com:nate/project.git")
        clone = self.repository("phase-copy", "https://github.com/nate/project.git")
        self.assertEqual(self.claim("linux", main, 101)[0], 0)
        self.assertEqual(self.claim("linux", clone, 102)[0], 0)

        self.assertEqual(self.claim("linux", main, 103)[0], 11)
        self.assertEqual(self.claim("linux", clone, 104)[0], 11)

    def test_mac_serializes_different_origins_with_the_same_name(self) -> None:
        first = self.repository("first/project", "https://github.com/first/project.git")
        second = self.repository("second/project", "https://github.com/second/project.git")

        self.assertNotEqual(offload.repository_identity(first), offload.repository_identity(second))
        self.assertEqual(self.claim("mac", first, 101)[0], 0)
        self.assertEqual(self.claim("mac", second, 102)[0], 11)
        self.assertEqual(set(self.claims("mac")), {101})
        self.release("mac", 101)
        self.assertEqual(self.claim("mac", second, 102)[0], 0)

    def test_linux_keeps_same_name_origins_independent(self) -> None:
        first = self.repository("first/project", "https://github.com/first/project.git")
        second = self.repository("second/project", "https://github.com/second/project.git")
        for pid in (101, 102):
            self.assertEqual(self.claim("linux", first, pid)[0], 0)
        self.assertEqual(self.claim("linux", first, 103)[0], 11)

        self.assertEqual(self.claim("linux", second, 103)[0], 0)
        self.assertEqual(set(self.claims("linux")), {101, 102, 103})

    def test_local_and_shared_clones_keep_the_original_identity(self) -> None:
        main = self.repository("hana", "https://github.com/nate/hana.git")
        local = self.repository("tmp/local-clone", str(main))
        shared = self.repository("tmp/shared-clone", str(local))
        alternates = shared / ".git/objects/info/alternates"
        alternates.parent.mkdir(parents=True)
        _ = alternates.write_text(str(main / ".git/objects") + "\n")

        expected = offload.repository_identity(main)

        self.assertEqual(offload.repository_identity(local), expected)
        self.assertEqual(offload.repository_identity(shared), expected)

    def test_credentials_and_default_ports_do_not_change_identity(self) -> None:
        first = self.repository("one/project", "https://user@example.com:443/team/project.git?view=1#readme")
        second = self.repository("two/project", "ssh://git@example.com:22/team/project.git")
        third = self.repository("three/project", "https://example.com:8443/team/project.git")

        self.assertEqual(offload.repository_identity(first), offload.repository_identity(second))
        self.assertNotEqual(offload.repository_identity(second), offload.repository_identity(third))

    def test_concurrent_requests_never_share_a_slot(self) -> None:
        root = self.repository("project", "https://github.com/nate/project.git")
        barrier = threading.Barrier(6)

        def contend(pid: int) -> int:
            _ = barrier.wait(timeout=5)
            return mac_test.command_claim(self.arguments("linux", root, pid), mac_test.state_paths("linux"))

        with redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=6) as workers:
            statuses = list(workers.map(contend, range(101, 107)))

        self.assertEqual(sorted(statuses), [0, 0, 11, 11, 11, 11])
        records = list(self.claims("linux").values())
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record.get("slot") for record in records}), 2)

    def test_repository_override_and_config_reload_control_capacity(self) -> None:
        root = self.repository("phase-copy", "https://github.com/nate/hana.git")
        settings = self.root / "linux.conf"
        for pid in range(101, 105):
            self.assertEqual(self.claim("linux", root, pid)[0], 0)
        self.assertEqual(self.claim("linux", root, 105)[0], 11)
        _ = settings.write_text(settings.read_text().replace("slots.hana=4", "slots.hana=5"))

        self.assertEqual(self.claim("linux", root, 105)[0], 0)
        self.assertEqual(len(self.claims("linux")), 5)

    def test_free_slot_prefers_the_last_worktree(self) -> None:
        first = self.repository("project", "https://github.com/nate/project.git")
        second = self.repository("feature", "https://github.com/nate/project.git", first)
        self.assertEqual(self.claim("linux", first, 101)[0], 0)
        self.assertEqual(self.claim("linux", second, 102)[0], 0)
        preferred = self.claims("linux")[102].get("slot")
        self.release("linux", 101)
        self.release("linux", 102)

        self.assertEqual(self.claim("linux", second, 103)[0], 0)
        self.assertEqual(self.claims("linux")[103].get("slot"), preferred)

    def test_occupied_affinity_slot_is_never_reused(self) -> None:
        first = self.repository("project", "https://github.com/nate/project.git")
        second = self.repository("feature", "https://github.com/nate/project.git", first)
        self.assertEqual(self.claim("linux", first, 101)[0], 0)
        self.assertEqual(self.claim("linux", second, 102)[0], 0)
        occupied = self.claims("linux")[102].get("slot")
        self.release("linux", 101)

        self.assertEqual(self.claim("linux", second, 103)[0], 0)
        self.assertNotEqual(self.claims("linux")[103].get("slot"), occupied)

    def test_remote_claims_remain_when_the_local_owner_ends(self) -> None:
        root = self.repository("project", "https://github.com/nate/project.git")
        for pid in (101, 102):
            self.assertEqual(self.claim("linux", root, pid, remote=True)[0], 0)
        before = self.claims("linux")
        self.dead.update((101, 102))

        self.assertEqual(self.claim("linux", root, 103)[0], 11)
        self.assertEqual(self.claims("linux"), before)
        self.release("linux", 101)
        self.assertEqual(self.claim("linux", root, 103)[0], 0)
        self.assertIn(102, self.claims("linux"))

    def test_repeated_claim_preserves_its_occupied_slot(self) -> None:
        root = self.repository("project", "https://github.com/nate/project.git")
        settings = self.root / "linux.conf"
        _ = settings.write_text(settings.read_text().replace("slots=2", "slots=1"))
        requested = claim_state.ClaimIdentity("claim-a", "episode-a", "attempt-a")
        self.assertEqual(self.claim("linux", root, 101, request_identity=requested)[0], 0)
        before = self.claims("linux")

        self.assertEqual(self.claim("linux", root, 101, request_identity=requested)[0], 0)

        self.assertEqual(self.claims("linux"), before)
        for other in (claim_state.ClaimIdentity("claim-b", "episode-a", "attempt-a"),
                      claim_state.ClaimIdentity("claim-a", "episode-b", "attempt-a"),
                      claim_state.ClaimIdentity("claim-a", "episode-a", "attempt-b")):
            with self.subTest(identity=other):
                self.assertEqual(self.claim("linux", root, 101, request_identity=other)[0], 11)
                self.assertEqual(self.claims("linux"), before)

    def test_takeover_keeps_the_remote_slot_until_cleanup(self) -> None:
        settings = self.root / "linux.conf"
        _ = settings.write_text(settings.read_text().replace("slots=2", "slots=1"))
        root = self.repository("project", "https://github.com/nate/project.git")
        self.assertEqual(self.claim("linux", root, 101, remote=True)[0], 0)
        before = self.claims("linux")[101]
        self.dead.add(101)

        status, output = self.claim("linux", root, 102, remote=True, identity=True)

        self.assertEqual(status, 13)
        answer = offload.read_claim_answer(offload.CommandOutput(status, output, ""))
        self.assertIsInstance(answer, offload.TakenOverMacClaim)
        self.assertEqual(set(self.claims("linux")), {102})
        retained = self.claims("linux")[102]
        self.assertEqual(retained.get("slot"), before.get("slot"))
        self.assertEqual(retained.get("remote_host"), before.get("remote_host"))
        if isinstance(answer, offload.TakenOverMacClaim):
            self.assertEqual(answer.slot, before.get("slot"))

    def test_dead_request_cannot_replace_slot_affinity(self) -> None:
        root = self.repository("project", "https://github.com/nate/project.git")
        self.assertEqual(self.claim("linux", root, 101)[0], 0)
        self.release("linux", 101)
        paths = mac_test.state_paths("linux")
        before = {str(path.relative_to(paths.directory)): path.read_bytes()
                  for path in paths.directory.rglob("*.json")}
        self.dead.add(102)

        self.assertEqual(self.claim("linux", root, 102)[0], 1)
        after = {str(path.relative_to(paths.directory)): path.read_bytes()
                 for path in paths.directory.rglob("*.json")}
        self.assertEqual(after, before)

    def test_claim_answer_preserves_the_allocated_slot(self) -> None:
        root = self.repository("project", "https://github.com/nate/project.git")
        for pid in (101, 102):
            status, output = self.claim("linux", root, pid, identity=True)
            answer = offload.read_claim_answer(offload.CommandOutput(status, output, ""))

            self.assertIsInstance(answer, offload.FreshMacClaim)
            if isinstance(answer, offload.FreshMacClaim):
                self.assertEqual(answer.slot, self.claims("linux")[pid].get("slot"))

    def test_linux_mirrors_use_the_build_folder_rule_without_percent_encoding_the_key(self) -> None:
        cases = (
            (offload.KnownRepository("checkout", "https://example.com/one/hana"), "hana-33d435bf"),
            (offload.KnownRepository("checkout", "https://example.com/two/hana"), "hana-020748b0"),
            (offload.KnownRepository("project", "git-root:" + "1" * 40), "project-c8446dca"),
        )
        mirrors: set[str] = set()
        for repository, name in cases:
            with self.subTest(repository=repository):
                mirror = offload.remote_mirror(repository, "linux", 2)
                self.assertEqual(mirror, f".local/state/build-target/mirror/{name}/linux-s2")
                self.assertNotIn("%", mirror)
                self.assertIn("%", repository.path_key)
                mirrors.add(mirror)
        self.assertEqual(len(mirrors), len(cases))
        self.assertEqual(offload.remote_mirror(offload.KnownRepository("other-copy", cases[0][0].key), "linux", 2),
                         offload.remote_mirror(cases[0][0], "linux", 2))

    def test_linux_mirrors_isolate_origins_slots_and_build_folders(self) -> None:
        first = self.repository("one/project", "ssh://git@example.com/a/project.git")
        second = self.repository("two/project", "https://example.com/b/project.git")
        first_id = offload.repository_identity(first)
        second_id = offload.repository_identity(second)
        self.assertIsInstance(first_id, offload.KnownRepository)
        self.assertIsInstance(second_id, offload.KnownRepository)
        if not isinstance(first_id, offload.KnownRepository) or not isinstance(second_id, offload.KnownRepository):
            self.fail("Repository identity must resolve before selecting a mirror")
        mirrors = [offload.remote_mirror(first_id, "linux", 1), offload.remote_mirror(first_id, "linux", 2),
                   offload.remote_mirror(second_id, "linux", 1)]

        self.assertEqual(len(set(mirrors)), 3)
        for mirror, slot in zip(mirrors, (1, 2, 1), strict=True):
            path = PurePosixPath(mirror)
            self.assertEqual(path.parts[:4], (".local", "state", "build-target", "mirror"))
            self.assertEqual(len(path.parts), 6)
            self.assertEqual(path.name, f"linux-s{slot}")
            self.assertNotIn("..", path.parts)
        self.assertEqual(offload.remote_mirror(first_id), ".local/state/mac-test/mirror/project")
        config = offload.read_config(self.root / "linux.conf")
        request = offload.OffloadRequest(first, "project", "call", offload.NO_RESULT_FILE, (), owner="verify", target="linux", slot=2, holder_session="fixture-session")

        command = offload.remote_test_command(config, request, first_id)

        self.assertIn(mirrors[1], command)
        words = shlex.split(command)
        self.assertEqual(words[words.index("--mirror") + 1], mirrors[1])
        self.assertNotIn(mirrors[0], words)

    def test_invalid_slot_answers_never_confirm_a_claim(self) -> None:
        for output in ("claimed\nslot: 0\n", "claimed\nslot: -1\n", "claimed\nslot: x\n",
                       "claimed\nslot: 1\nslot: 2\n"):
            with self.subTest(output=output):
                output += 'claim_identity={"claim_id":"claim-a","episode":"episode-a","attempt":"attempt-a"}\n'
                answer = offload.read_claim_answer(offload.CommandOutput(0, output, ""))
                self.assertIsInstance(answer, offload.MacClaimNotConfirmed)

    def test_origin_name_does_not_move_a_legacy_mac_mirror_or_claim(self) -> None:
        root = self.repository(".claude", "https://github.com/nate/claude_commands.git")
        identity = offload.repository_identity(root)
        self.assertIsInstance(identity, offload.KnownRepository)
        if not isinstance(identity, offload.KnownRepository):
            self.fail("The repository must have a known identity")
        self.assertEqual(identity.name, ".claude")
        self.assertEqual(identity.key, "https://github.com/nate/claude_commands")
        self.assertEqual(offload.remote_mirror(identity), ".local/state/mac-test/mirror/.claude")
        paths = mac_test.state_paths("mac")
        path = paths.claims / ".claude.json"
        path.parent.mkdir(parents=True)
        _ = path.write_text(json.dumps({
            "claim_id": "claim-a", "episode": "episode-a", "attempt": "attempt-a", "submitting_session": "",
            "pid": 101, "proc_start": "start-101", "worktree": str(root), "what": "legacy run",
            "since": "2026-11-01T00:00:00+00:00",
        }))

        self.assertEqual(self.claim("mac", root, 102)[0], 11)
        self.assertEqual(list(paths.claims.iterdir()), [path])

    def test_takeover_retry_requires_cleanup_before_a_fresh_claim(self) -> None:
        settings = self.root / "linux.conf"
        _ = settings.write_text(settings.read_text().replace("slots=2", "slots=1"))
        root = self.repository("project", "https://github.com/nate/project.git")
        self.assertEqual(self.claim("linux", root, 101, remote=True)[0], 0)
        self.dead.add(101)
        status, output = self.claim("linux", root, 102, remote=True, identity=True)
        self.assertEqual(status, 13)

        requested = claim_state.reply_claim_identity(output)
        repeated_status, repeated_output = self.claim(
            "linux", root, 102, remote=True, identity=True, request_identity=requested)

        self.assertEqual(repeated_status, 13)
        self.assertEqual(repeated_output, output)
        self.assertIsInstance(offload.read_claim_answer(
            offload.CommandOutput(repeated_status, repeated_output, "")), offload.TakenOverMacClaim)
        self.release("linux", 102)
        self.assertEqual(self.claim("linux", root, 102, remote=True)[0], 0)

    def test_another_worktree_can_clean_full_slots_after_the_owners_end(self) -> None:
        source = self.repository("project", "https://github.com/nate/project.git")
        abandoned = [self.repository(name, "https://github.com/nate/project.git", source)
                     for name in ("old-a", "old-b")]
        for pid, root in zip((101, 102), abandoned, strict=True):
            self.assertEqual(self.claim("linux", root, pid, remote=True)[0], 0)
            (root / ".git").unlink()
            root.rmdir()
        self.dead.update((101, 102))
        replacement = self.repository("replacement", "https://github.com/nate/project.git", source)

        status, output = self.claim("linux", replacement, 103, remote=True)

        self.assertEqual(status, 13)
        self.assertIn("leftovers: fake-linux", output)
        taken = self.claims("linux")[103]
        self.assertEqual(taken.get("worktree"), str(abandoned[0]))
        self.assertIs(taken.get("cleanup_pending"), True)
        self.assertEqual(len(self.claims("linux")), 2)

    def test_affinity_writes_remove_deleted_worktrees(self) -> None:
        source = self.repository("project", "https://github.com/nate/project.git")
        removed = self.repository("removed", "https://github.com/nate/project.git", source)
        self.assertEqual(self.claim("linux", removed, 101)[0], 0)
        self.release("linux", 101)
        (removed / ".git").unlink()
        removed.rmdir()

        self.assertEqual(self.claim("linux", source, 102)[0], 0)

        paths = mac_test.state_paths("linux")
        affinity = cast(dict[str, object], json.loads(next(paths.affinity.iterdir()).read_text()))
        self.assertNotIn(str(removed), affinity)
        self.assertIn(str(source), affinity)

    def test_target_platform_selects_the_mirror_and_claim_layout(self) -> None:
        registry = self.root / "build_targets.conf"
        _ = registry.write_text(registry.read_text().replace(
            "targets=mac,linux,natedev", "targets=mac,linux,darwin,natedev")
            + "kind.darwin=ssh\nplatform.darwin=macos\nsettings.darwin=mac.conf\n")
        root = self.repository("project", "https://github.com/nate/project.git")
        identity = offload.repository_identity(root)
        if not isinstance(identity, offload.KnownRepository):
            self.fail("The repository must have a known identity")
        self.assertEqual(offload.remote_mirror(identity, "darwin"), ".local/state/mac-test/mirror/project")
        self.assertEqual(self.claim("darwin", root, 101)[0], 0)
        self.assertEqual(self.claim("darwin", root, 102)[0], 11)
        self.assertTrue((mac_test.state_paths("darwin").claims / "project.json").exists())
        _ = registry.write_text(registry.read_text().replace("platform.mac=macos", "platform.mac=linux"))

        self.assertTrue(offload.remote_mirror(identity, "mac", 2).endswith("/mac-s2"))
        self.assertEqual(self.claim("mac", root, 103)[0], 0)
        self.assertIn("slot", self.claims("mac")[103])

    def test_target_state_includes_every_active_slot(self) -> None:
        registry = self.root / "build_targets.conf"
        _ = registry.write_text(registry.read_text().replace('targets=mac,linux,', 'targets=mac,linux-one,').replace('.linux=', '.linux-one=').replace('=linux.conf', '=linux-one.conf'))
        _ = (self.root / "linux.conf").rename(self.root / "linux-one.conf")
        root = self.repository("project", "https://github.com/nate/project.git")
        for pid in (101, 102):
            self.assertEqual(self.claim("linux-one", root, pid)[0], 0)
        configured = next(target for target in targets.read_targets() if target.name == "linux-one")

        state = targets.target_state(configured)

        self.assertIsInstance(state, targets.Running)
        if isinstance(state, targets.Running):
            self.assertEqual(len(state.jobs), 2)

    def test_linux_origin_slot_override_applies_to_differently_named_clones(self) -> None:
        origin = "https://github.com/nate/hana.git"
        main = self.repository("hana", origin)
        clone = self.repository("phase-copy", origin)
        settings = self.root / "linux.conf"
        _ = settings.write_text(settings.read_text().replace("slots.hana=4", "slots.hana=3")
                                + "slots.phase-copy=1\n")

        for root, pid in ((clone, 101), (clone, 102), (main, 103)):
            self.assertEqual(self.claim("linux", root, pid)[0], 0)

        self.assertEqual(self.claim("linux", clone, 104)[0], 11)
        self.assertEqual(self.claim("linux", main, 104)[0], 11)
        self.assertEqual(len({record.get("slot") for record in self.claims("linux").values()}), 3)

    def test_mac_skip_keeps_legacy_filters_when_linux_filter_support_expands(self) -> None:
        identity = offload.KnownRepository("hana", "https://github.com/nate/hana")
        settings = self.root / "mac.conf"
        _ = write_config_fixture(settings, settings.read_text() + "mac_skip.hana=test(gpu)\n",
                                 template="mac_test")
        cases = (
            (("-E", "test(a)"), ("-E", "(test(a)) & not (test(gpu))")),
            (("--workspace",), ("--workspace",)),
            (("-E",), ("-E",)),
            (("--filterset=test(a)",), ("--filterset=test(a)",)),
            (("-E=test(a)",), ("-E=test(a)",)),
        )
        for words, expected in cases:
            with self.subTest(words=words):
                request = offload.OffloadRequest(self.root / "hana", "hana", "call", offload.NO_RESULT_FILE, words, owner="verify", holder_session="fixture-session")

                command = offload.remote_test_command(offload.read_config(settings), request, identity)

                invocation = shlex.split(command.rpartition("; echo mac_exit=$?")[0])
                start = invocation.index("~/.claude/scripts/lint/lint") + 1
                self.assertEqual(invocation[start:], ["nextest", *expected])

    def test_deleted_worktree_takeover_preserves_the_other_occupied_slot(self) -> None:
        source = self.repository("project", "https://github.com/nate/project.git")
        old = self.repository("deleted", "https://github.com/nate/project.git", source)
        self.assertEqual(self.claim("linux", old, 101, remote=True)[0], 0)
        self.assertEqual(self.claim("linux", source, 102, remote=True)[0], 0)
        before = self.claims("linux")
        (old / ".git").unlink()
        old.rmdir()
        self.dead.add(101)

        self.assertEqual(self.claim("linux", source, 103, remote=True)[0], 13)
        retained = self.claims("linux")[103]
        self.assertEqual(retained.get("slot"), before[101].get("slot"))
        self.assertEqual(retained.get("repo_key"), before[101].get("repo_key"))
        self.assertEqual(retained.get("repo_name"), before[101].get("repo_name"))
        self.assertEqual(retained.get("worktree"), str(old))
        self.assertIs(retained.get("cleanup_pending"), True)
        requested = claim_state.ClaimIdentity(cast(str, retained["claim_id"]),
                                               cast(str, retained["episode"]), cast(str, retained["attempt"]))
        self.assertEqual(self.claim("linux", source, 103, remote=True, request_identity=requested)[0], 13)
        self.assertEqual(self.claim("linux", source, 104, remote=True)[0], 11)
        self.assertEqual(self.claims("linux")[102], before[102])

        self.release("linux", 103)
        self.assertEqual(self.claim("linux", source, 104, remote=True)[0], 0)
        self.assertEqual(self.claims("linux")[104].get("slot"), before[101].get("slot"))
        self.assertEqual(self.claims("linux")[102], before[102])


if __name__ == "__main__":
    _ = unittest.main()
